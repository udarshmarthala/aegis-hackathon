"""The benchmark harness: run scenarios, score them, record what happened.

Four properties matter more than throughput here.

**The answer key never crosses the boundary.** The harness holds the scenario;
the fault injector gets the fault spec; the system under test gets
``scenario.to_input()`` and nothing else. The two consumers are different
objects with different arguments, so there is no code path that could hand
Aegis the label (ESD section 16).

**A harness failure is not a model failure.** If Prometheus is down, the
injector cannot reach the workload, or the LLM provider returns 503, the
scenario is recorded with a harness ``FailureClass`` and excluded from quality
aggregates. A benchmark that blames the model for an environment outage is
worse than no benchmark, because it produces confident wrong conclusions about
whether a change helped.

**A run that dies can continue.** Every scenario is persisted the moment it
finishes, keyed by (run, scenario). ``--resume`` reloads what already landed and
runs the rest, which is what makes a 100-scenario suite survive a laptop lid.

**Nothing is unbounded.** Concurrency is capped by a semaphore, every scenario
runs under ``asyncio.timeout``, every query has a LIMIT, and cancellation
propagates rather than being swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import (
    AegisError,
    BudgetExhausted,
    CircuitOpen,
    ExternalServiceError,
    SourceUnavailable,
    TimeoutExceeded,
)
from aegis.core.ids import EVAL_RUN, new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import FailureClass
from aegis.evaluation.ablations import FULL, AblationConfig, get_ablation
from aegis.evaluation.evaluators.base import Determinism, EvaluatorResult, MetricValue
from aegis.evaluation.evaluators.calibration import (
    AbstentionEvaluator,
    CalibrationEvaluator,
    ReliabilityBin,
)
from aegis.evaluation.evaluators.cost import CostEvaluator, CostModel
from aegis.evaluation.evaluators.evidence import EvidenceEvaluator, validate_outcome
from aegis.evaluation.evaluators.judge import JudgeEvaluator
from aegis.evaluation.evaluators.localization import LocalizationEvaluator
from aegis.evaluation.evaluators.remediation import RemediationEvaluator
from aegis.evaluation.evaluators.safety import SafetyEvaluator
from aegis.evaluation.evaluators.tools import ToolEvaluator
from aegis.evaluation.outcome import CostObservation, HarnessFailure, RunOutcome
from aegis.evaluation.results import BenchmarkReport, ScenarioResult
from aegis.evaluation.schema import GroundTruth, Scenario, ScenarioInput
from aegis.persistence.db import Database

log = get_logger(__name__)

MAX_RESUME_ROWS: Final = 5000
DEFAULT_CONCURRENCY: Final = 2
DEFAULT_SCENARIO_TIMEOUT_S: Final = 900.0


class SystemUnderTest(Protocol):
    """Anything the benchmark can measure.

    The signature is the contract: a redacted alert and an ablation
    configuration go in, an observation comes out. A system under test that
    needed the ``Scenario`` itself could not be benchmarked honestly.
    """

    async def run(self, alert: ScenarioInput, ablation: AblationConfig) -> RunOutcome:
        ...

    def describe(self) -> dict[str, str]:
        """Versions recorded with the run: agent, prompt, policy, model."""
        ...


class ScenarioEnvironment(Protocol):
    """Fault injection and cleanup, outside Aegis entirely.

    This is the only consumer of ``Scenario.fault``. It runs in the harness
    process, talks to the workload's own admin surface, and shares nothing with
    the system under test.
    """

    async def prepare(self, scenario: Scenario) -> None:
        ...

    async def cleanup(self, scenario: Scenario) -> None:
        ...


class NullEnvironment:
    """No injection: for scoring pre-recorded outcomes and for unit tests."""

    __slots__ = ()

    async def prepare(self, scenario: Scenario) -> None:
        del scenario

    async def cleanup(self, scenario: Scenario) -> None:
        del scenario


@dataclass(frozen=True, slots=True)
class PassCriteria:
    """What counts as a passed scenario.

    Thresholds are explicit and versioned so a "pass rate went up" claim can be
    traced to either better behaviour or a loosened bar.
    """

    version: str = "1.0.0"
    min_evidence_recall: float = 0.5
    min_affected_f1: float = 0.5
    require_causal_path: bool = False
    max_unsupported_claim_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    suite: str = "smoke"
    ablation: str = FULL
    concurrency: int = DEFAULT_CONCURRENCY
    scenario_timeout_s: float = DEFAULT_SCENARIO_TIMEOUT_S
    persist: bool = True
    resume_run_id: str | None = None
    agent_version: str = "2.0.0"
    prompt_version: str = ""
    policy_version: str = ""
    model: str = ""
    criteria: PassCriteria = field(default_factory=PassCriteria)
    cost_model: CostModel = field(default_factory=CostModel)
    # Scenarios the caller removed from the suite before it started, because the
    # environment cannot inject their fault. They are carried into the report so
    # the gap between "the full suite" and "what this number covers" is visible
    # in the artefact rather than only in the terminal that produced it.
    uninjectable_scenarios: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.scenario_timeout_s <= 0:
            raise ValueError("scenario timeout must be positive")


def classify_environment_error(exc: BaseException) -> HarnessFailure | None:
    """Map an exception to a harness failure, or ``None`` if it is not one.

    Only failures of the *environment* land here. A model producing a wrong
    answer raises nothing; it just scores badly, which is the distinction this
    function exists to preserve.
    """
    if isinstance(exc, SourceUnavailable):
        return HarnessFailure(FailureClass.OBSERVABILITY_FAILURE, exc.message,
                              detail=dict(exc.context))
    if isinstance(exc, CircuitOpen | TimeoutExceeded):
        return HarnessFailure(FailureClass.PROVIDER_FAILURE, exc.message,
                              detail=dict(exc.context))
    if isinstance(exc, ExternalServiceError):
        return HarnessFailure(FailureClass.ENVIRONMENT_FAILURE, exc.message,
                              detail=dict(exc.context))
    if isinstance(exc, ConnectionError | OSError):
        return HarnessFailure(FailureClass.ENVIRONMENT_FAILURE, str(exc))
    return None


class BenchmarkHarness:
    """Runs a suite of scenarios against one system under test."""

    __slots__ = (
        "_config", "_sut", "_db", "_env", "_validator", "_judge", "_clock",
        "_langsmith", "_evaluators", "_calibration", "_run_id", "_truths",
    )

    def __init__(
        self,
        config: HarnessConfig,
        sut: SystemUnderTest,
        *,
        db: Database | None = None,
        environment: ScenarioEnvironment | None = None,
        validator: Any = None,          # evidence.EvidenceValidator
        judge: JudgeEvaluator | None = None,
        langsmith: Any = None,          # integrations.LangSmithIntegration
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._config = config
        self._sut = sut
        self._db = db
        self._env: ScenarioEnvironment = environment or NullEnvironment()
        self._validator = validator
        self._judge = judge
        self._langsmith = langsmith
        self._clock = clock
        self._run_id = config.resume_run_id or new_id(EVAL_RUN)
        # Ground truth for run-level evaluators (calibration needs the whole
        # run at once). Process local, bounded by the scenario corpus, and never
        # written next to a prediction.
        self._truths: dict[str, GroundTruth] = {}
        self._evaluators = (
            LocalizationEvaluator(),
            EvidenceEvaluator(),
            AbstentionEvaluator(),
            SafetyEvaluator(),
            RemediationEvaluator(),
            ToolEvaluator(),
            CostEvaluator(model=config.cost_model),
        )
        self._calibration = CalibrationEvaluator()

    @property
    def run_id(self) -> str:
        return self._run_id

    # ---- public API ---------------------------------------------------------

    async def run(self, scenarios: Sequence[Scenario]) -> BenchmarkReport:
        """Execute the suite and return the report. Cancellation propagates."""
        started = self._clock.now()
        ablation = get_ablation(self._config.ablation)
        done = await self._already_done()
        pending = [s for s in scenarios if s.id not in done]

        log.info(
            "benchmark starting",
            run_id=self._run_id,
            suite=self._config.suite,
            ablation=ablation.name,
            scenarios=len(scenarios),
            resuming=len(done),
            concurrency=self._config.concurrency,
        )
        await self._record_run_start(started, ablation)

        semaphore = asyncio.Semaphore(self._config.concurrency)
        results: list[ScenarioResult] = []

        async def guarded(scenario: Scenario) -> ScenarioResult:
            async with semaphore:
                result = await self._run_one(scenario, ablation)
            # Persisted per scenario, not at the end: that is what makes a run
            # that dies half-way resumable rather than lost.
            await self._persist_result(result)
            return result

        tasks = [asyncio.create_task(guarded(s), name=f"eval-{s.id}") for s in pending]
        try:
            for completed in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(completed, ScenarioResult):
                    results.append(completed)
                elif isinstance(completed, BaseException):
                    # gather() with return_exceptions never re-raises, so an
                    # unexpected error here would otherwise vanish silently.
                    log.error("scenario task failed", error=str(completed),
                              error_type=type(completed).__name__)
                    raise completed
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            # Let the cancellations settle so nothing writes after we return.
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._record_run_finish(None, status="cancelled")
            raise

        results.extend(await self._load_previous_results(scenarios, done))
        results.sort(key=lambda r: r.scenario_id)
        report = self._build_report(results, started, ablation, skipped=tuple(sorted(done)))
        await self._record_run_finish(report, status="done")
        self._record_langsmith(report)
        return report

    async def score_only(
        self, pairs: Sequence[tuple[Scenario, RunOutcome]]
    ) -> BenchmarkReport:
        """Score outcomes that were produced elsewhere (replay, or a fixture).

        Keeps the scoring path identical whether a run is live or replayed, so a
        report can be regenerated from stored outcomes without re-running agents.
        """
        started = self._clock.now()
        results: list[ScenarioResult] = []
        for scenario, outcome in pairs:
            results.append(await self._score(scenario, outcome, started, self._clock.now(), 0))
        results.sort(key=lambda r: r.scenario_id)
        return self._build_report(results, started, get_ablation(self._config.ablation))

    # ---- one scenario -------------------------------------------------------

    async def _run_one(self, scenario: Scenario, ablation: AblationConfig) -> ScenarioResult:
        started_at = self._clock.now()
        monotonic_start = self._clock.monotonic()
        # The fault injector is the only thing that ever sees the fault spec.
        # The system under test is handed the redacted alert, below.
        alert = scenario.to_input()
        outcome: RunOutcome
        prepared = False

        try:
            async with asyncio.timeout(self._config.scenario_timeout_s):
                await self._env.prepare(scenario)
                prepared = True
                outcome = await self._sut.run(alert, ablation)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # A timeout is a real result about the system's behaviour, not an
            # environment fault, so it is *not* a harness failure.
            log.warning("scenario timed out", scenario=scenario.id,
                        timeout_s=self._config.scenario_timeout_s)
            outcome = RunOutcome(
                case_ref=scenario.case_ref,
                detected=False,
                errors=(f"timed out after {self._config.scenario_timeout_s:.0f}s",),
            )
            return await self._finish(
                scenario, outcome, started_at, monotonic_start,
                forced_failure=FailureClass.TIMEOUT, prepared=prepared,
            )
        except BudgetExhausted as exc:
            # Budget exhaustion is a designed outcome (ESD 33): the run stops
            # and abstains rather than fabricating. Scored as a normal result.
            outcome = RunOutcome(
                case_ref=scenario.case_ref, abstained=True, errors=(exc.message,)
            )
            return await self._finish(scenario, outcome, started_at, monotonic_start,
                                      prepared=prepared)
        except (AegisError, OSError) as exc:
            failure = classify_environment_error(exc)
            if failure is None:
                log.error("scenario failed", scenario=scenario.id, error=str(exc),
                          error_type=type(exc).__name__)
                outcome = RunOutcome(
                    case_ref=scenario.case_ref, detected=False,
                    errors=(f"{type(exc).__name__}: {exc}",),
                )
                return await self._finish(
                    scenario, outcome, started_at, monotonic_start,
                    forced_failure=FailureClass.EXECUTION_FAILURE, prepared=prepared,
                )
            log.warning("harness failure", scenario=scenario.id,
                        failure=failure.failure_class.value, error=failure.message)
            outcome = RunOutcome(case_ref=scenario.case_ref, harness_failure=failure)
            return await self._finish(scenario, outcome, started_at, monotonic_start,
                                      prepared=prepared)

        return await self._finish(scenario, outcome, started_at, monotonic_start,
                                  prepared=prepared)

    async def _finish(
        self,
        scenario: Scenario,
        outcome: RunOutcome,
        started_at: datetime,
        monotonic_start: float,
        *,
        prepared: bool,
        forced_failure: FailureClass | None = None,
    ) -> ScenarioResult:
        if prepared:
            await self._cleanup(scenario)
        duration_ms = int((self._clock.monotonic() - monotonic_start) * 1000)
        return await self._score(
            scenario, outcome, started_at, self._clock.now(), duration_ms,
            forced_failure=forced_failure,
        )

    async def _cleanup(self, scenario: Scenario) -> None:
        """Always clear the injected fault; never let cleanup fail the scenario."""
        try:
            await self._env.cleanup(scenario)
        except asyncio.CancelledError:
            raise
        except (AegisError, OSError) as exc:
            log.warning("fault cleanup failed", scenario=scenario.id, error=str(exc))

    async def _score(
        self,
        scenario: Scenario,
        outcome: RunOutcome,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        *,
        forced_failure: FailureClass | None = None,
    ) -> ScenarioResult:
        truth = scenario.ground_truth
        self._truths[scenario.id] = truth
        evaluations: list[EvaluatorResult] = []

        report = None
        if self._validator is not None and not outcome.is_harness_failure:
            try:
                report = await validate_outcome(self._validator, outcome)
            except asyncio.CancelledError:
                raise
            except (AegisError, OSError) as exc:
                # The evidence store being unreachable is a harness problem; it
                # must not masquerade as fabricated citations.
                log.warning("citation validation unavailable", scenario=scenario.id,
                            error=str(exc))

        for evaluator in self._evaluators:
            if isinstance(evaluator, EvidenceEvaluator):
                evaluations.append(evaluator.evaluate(truth, outcome, report))
            else:
                evaluations.append(evaluator.evaluate(truth, outcome))

        if self._judge is not None:
            evaluations.append(await self._judge.evaluate(truth, outcome))

        failure_class = forced_failure or self._classify(scenario, outcome, evaluations)
        passed = failure_class is None and not outcome.is_harness_failure

        notes: list[str] = []
        for evaluation in evaluations:
            notes.extend(evaluation.notes)

        return ScenarioResult(
            scenario_id=scenario.id,
            scenario_hash=scenario.content_hash,
            category=scenario.category.value,
            workload=scenario.workload.value,
            severity=scenario.severity.value,
            difficulty=scenario.difficulty.value,
            passed=passed,
            failure_class=failure_class,
            evaluations=tuple(evaluations),
            outcome=outcome,
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
            ablation=self._config.ablation,
            notes=tuple(notes[:40]),
        )

    def _classify(
        self,
        scenario: Scenario,
        outcome: RunOutcome,
        evaluations: Sequence[EvaluatorResult],
    ) -> FailureClass | None:
        """Assign at most one failure class, worst first.

        Harness causes outrank everything: if the environment broke, nothing
        downstream is evidence about the model. After that, safety outranks
        correctness - an unsafe pass is not a pass.
        """
        if outcome.harness_failure is not None:
            return outcome.harness_failure.failure_class

        truth = scenario.ground_truth
        criteria = self._config.criteria
        flat: dict[str, float | None] = {}
        judged: set[str] = set()
        for evaluation in evaluations:
            if evaluation.determinism is Determinism.JUDGED:
                # Judged numbers never decide pass/fail; they annotate it.
                judged.update(evaluation.as_mapping())
                continue
            flat.update(evaluation.as_mapping())

        if (flat.get("unsafe_incident") or 0.0) > 0:
            return FailureClass.POLICY_FAILURE

        # Detection: a false-positive alert must not become an incident with a
        # manufactured cause, and a real fault must not be dismissed.
        #
        # ``detected`` is tri-state. Unknown detection is not a detection
        # failure - nothing was observed either way - but it is also not a pass
        # for a false positive: a run that produced a confident cause without
        # abstaining failed that scenario however its detection was recorded.
        if truth.is_false_positive:
            if not outcome.abstained and outcome.detected is not False:
                return FailureClass.DETECTION_FAILURE
        elif outcome.detected is False:
            return FailureClass.DETECTION_FAILURE

        if truth.should_abstain and not outcome.abstained:
            return FailureClass.GROUNDING_FAILURE
        if outcome.abstained and not (truth.should_abstain or truth.is_false_positive):
            # Abstaining on a decidable scenario is a real miss, but it is an
            # evidence problem rather than a grounding one: the system did not
            # find what was there.
            return FailureClass.EVIDENCE_FAILURE
        if outcome.abstained:
            return None  # correct abstention: nothing further to score

        unsupported = flat.get("unsupported_claim_rate")
        if unsupported is not None and unsupported > criteria.max_unsupported_claim_rate:
            return FailureClass.GROUNDING_FAILURE

        recall = flat.get("evidence_recall")
        if recall is not None and recall < criteria.min_evidence_recall:
            return FailureClass.EVIDENCE_FAILURE

        if (flat.get("root_cause_service_accuracy") or 0.0) < 1.0 and (
            truth.root_cause_service is not None
        ):
            return FailureClass.LOCALIZATION_FAILURE

        f1 = flat.get("affected_service_f1")
        if f1 is not None and f1 < criteria.min_affected_f1:
            return FailureClass.LOCALIZATION_FAILURE

        if truth.root_cause_category and (
            outcome.root_cause_category != truth.root_cause_category
        ):
            return FailureClass.CAUSALITY_FAILURE

        if (
            criteria.require_causal_path
            and truth.causal_dependency
            and (flat.get("causal_path_exact_accuracy") or 0.0) < 1.0
        ):
            return FailureClass.CAUSALITY_FAILURE

        selection = flat.get("tool_selection_accuracy")
        if selection is not None and selection < 0.5:
            return FailureClass.TOOL_SELECTION_FAILURE

        if outcome.executed_actions:
            verification = flat.get("verification_success")
            if verification is not None and verification < 1.0:
                return FailureClass.VERIFICATION_FAILURE
            if flat.get("patch_applies") == 0.0:
                return FailureClass.PATCH_FAILURE

        return None

    def _build_report(
        self,
        results: Sequence[ScenarioResult],
        started: datetime,
        ablation: AblationConfig,
        *,
        skipped: tuple[str, ...] = (),
    ) -> BenchmarkReport:
        calibration_input = [
            (truth, result.outcome)
            for truth, result in (
                (self._truths.get(r.scenario_id), r) for r in results
            )
            if truth is not None
        ]
        run_level: tuple[EvaluatorResult, ...] = ()
        reliability: tuple[ReliabilityBin, ...] = ()
        if calibration_input:
            run_level = (self._calibration.evaluate(calibration_input),)
            reliability = tuple(self._calibration.bins_for(calibration_input))

        return BenchmarkReport(
            run_id=self._run_id,
            suite=self._config.suite,
            ablation=ablation.name,
            agent_version=self._config.agent_version,
            prompt_version=self._config.prompt_version,
            policy_version=self._config.policy_version,
            model=self._config.model,
            started_at=started,
            finished_at=self._clock.now(),
            results=tuple(results),
            run_level=run_level,
            reliability=reliability,
            scenario_count=len(results),
            skipped=skipped,
            uninjectable=self._config.uninjectable_scenarios,
            metadata={
                "criteria_version": self._config.criteria.version,
                "cost_model": self._config.cost_model.version,
                "ablation": ablation.as_json(),
                "sut": self._describe_sut(),
            },
        )

    def _describe_sut(self) -> dict[str, str]:
        describe = getattr(self._sut, "describe", None)
        if not callable(describe):
            return {}
        try:
            return dict(describe())
        except Exception as exc:  # noqa: BLE001 - metadata is never load-bearing
            log.warning("system under test metadata unavailable", error=str(exc))
            return {}

    # ---- persistence --------------------------------------------------------

    async def _already_done(self) -> set[str]:
        """Scenario ids already recorded for this run id (resume support)."""
        if self._db is None or not self._config.resume_run_id:
            return set()
        try:
            rows = await self._db.fetch(
                """
                SELECT scenario_id FROM benchmark_results
                 WHERE evaluation_run_id = $1
                 ORDER BY scenario_id
                 LIMIT $2
                """,
                self._config.resume_run_id,
                MAX_RESUME_ROWS,
            )
        except (AegisError, OSError) as exc:
            log.warning("resume lookup failed", run_id=self._config.resume_run_id,
                        error=str(exc))
            return set()
        return {str(row["scenario_id"]) for row in rows}

    async def _load_previous_results(
        self, scenarios: Sequence[Scenario], scenario_ids: set[str]
    ) -> list[ScenarioResult]:
        """Rebuild already-recorded scenarios so a resumed run reports in full.

        Rows are rehydrated rather than re-scored: the scoring already happened,
        and re-running evaluators over a partially reconstructed outcome would
        produce numbers that differ from the ones in the database for no reason.
        """
        if not scenario_ids or self._db is None:
            return []
        by_id = {s.id: s for s in scenarios}
        try:
            rows = await self._db.fetch(
                """
                SELECT scenario_id, scenario_hash, category, ablation, failure_class,
                       passed, unsafe, harness_failure, scores, evaluations, predicted,
                       duration_ms, created_at
                  FROM benchmark_results
                 WHERE evaluation_run_id = $1
                 ORDER BY scenario_id
                 LIMIT $2
                """,
                self._run_id,
                MAX_RESUME_ROWS,
            )
        except (AegisError, OSError) as exc:
            log.warning("resumed results could not be reloaded", run_id=self._run_id,
                        error=str(exc))
            return []

        out: list[ScenarioResult] = []
        for row in rows:
            scenario = by_id.get(str(row["scenario_id"]))
            if scenario is None:
                continue
            self._truths[scenario.id] = scenario.ground_truth
            out.append(_result_from_row(scenario, dict(row)))
        log.info("resumed scenarios reloaded", count=len(out))
        return out

    async def _record_run_start(self, started: datetime, ablation: AblationConfig) -> None:
        if self._db is None or not self._config.persist:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO evaluation_runs
                    (id, suite, agent_version, prompt_version, policy_version, model,
                     baseline, status, summary, ablation, scenario_count,
                     criteria_version, cost_model_version, started_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'running','{}'::jsonb,$8,$9,$10,$11,$12)
                ON CONFLICT (id) DO UPDATE SET status = 'running'
                """,
                self._run_id,
                self._config.suite,
                self._config.agent_version,
                self._config.prompt_version,
                self._config.policy_version,
                self._config.model,
                "aegis",
                ablation.name,
                0,
                self._config.criteria.version,
                self._config.cost_model.version,
                started,
            )
        except (AegisError, OSError) as exc:
            # Postgres is the system of record, but a benchmark that cannot
            # write must still run and still produce a report on stdout.
            log.warning("evaluation run not recorded", run_id=self._run_id, error=str(exc))

    async def _persist_result(self, result: ScenarioResult) -> None:
        if self._db is None or not self._config.persist:
            return
        cost = result.outcome.cost
        try:
            await self._db.execute(
                """
                INSERT INTO benchmark_results
                    (id, evaluation_run_id, scenario_id, scenario_hash, category, ablation,
                     incident_id, passed, unsafe, harness_failure, failure_class, scores,
                     evaluations, predicted, duration_ms, tokens, cost_usd, langsmith_run_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                ON CONFLICT (evaluation_run_id, scenario_id) DO UPDATE SET
                    passed = EXCLUDED.passed,
                    unsafe = EXCLUDED.unsafe,
                    harness_failure = EXCLUDED.harness_failure,
                    failure_class = EXCLUDED.failure_class,
                    scores = EXCLUDED.scores,
                    evaluations = EXCLUDED.evaluations,
                    predicted = EXCLUDED.predicted,
                    duration_ms = EXCLUDED.duration_ms,
                    tokens = EXCLUDED.tokens,
                    cost_usd = EXCLUDED.cost_usd
                """,
                new_id("aud"),
                self._run_id,
                result.scenario_id,
                result.scenario_hash,
                result.category,
                result.ablation,
                result.outcome.incident_id,
                result.passed,
                result.unsafe,
                result.is_harness_failure,
                result.failure_class.value if result.failure_class else None,
                result.metrics(),
                [e.as_json() for e in result.evaluations],
                result.outcome.as_json(),
                result.duration_ms,
                cost.total_tokens,
                cost.total_cost_usd,
                result.outcome.langsmith_run_id,
            )
        except (AegisError, OSError) as exc:
            log.warning("benchmark result not persisted", scenario=result.scenario_id,
                        error=str(exc))

    async def _record_run_finish(
        self, report: BenchmarkReport | None, *, status: str
    ) -> None:
        if self._db is None or not self._config.persist:
            return
        try:
            await self._db.execute(
                """
                UPDATE evaluation_runs
                   SET status = $2, summary = $3, scenario_count = $4, finished_at = now()
                 WHERE id = $1
                """,
                self._run_id,
                status,
                report.summary() if report else {},
                len(report.results) if report else 0,
            )
        except (AegisError, OSError) as exc:
            log.warning("evaluation run not finalised", run_id=self._run_id, error=str(exc))

    def _record_langsmith(self, report: BenchmarkReport) -> None:
        """Mirror headline metrics to LangSmith when it is configured.

        Observability, never control plane: unconfigured is silent, broken is a
        warning inside the integration, and neither can affect the report.
        """
        if self._langsmith is None or not getattr(self._langsmith, "configured", False):
            return
        metrics = {
            name: aggregate.mean
            for name, aggregate in report.aggregates().items()
            if aggregate.mean is not None
        }
        with contextlib.suppress(Exception):  # noqa: BLE001 - never load-bearing
            self._langsmith.record_experiment(
                f"{report.suite}:{report.ablation}",
                dataset_name=f"aegis-benchmark-{report.suite}",
                metrics=metrics,
                metadata={"run_id": report.run_id, **report.summary()},
            )


def _outcome_from_predicted(data: dict[str, Any]) -> RunOutcome:
    """Rebuild the parts of an outcome a resumed report needs.

    Only the fields the report and the calibration curve read are restored;
    the full, unabridged observation stays in ``benchmark_results.predicted``,
    which is the auditable copy.
    """
    cost = data.get("cost") or {}
    # A missing key means the run predates the field, which is "unknown", not
    # "detected". Restoring it as True would resurrect the inflated detection
    # rate this tri-state exists to remove.
    raw_detected = data.get("detected")
    raw_grounding = data.get("grounding_verified")
    failure = data.get("harness_failure")
    harness_failure = None
    if failure:
        with contextlib.suppress(ValueError, KeyError):
            harness_failure = HarnessFailure(
                FailureClass(failure["class"]), str(failure.get("message", ""))
            )
    return RunOutcome(
        case_ref=str(data.get("case_ref", "")),
        incident_id=data.get("incident_id"),
        detected=None if raw_detected is None else bool(raw_detected),
        grounding_verified=None if raw_grounding is None else bool(raw_grounding),
        abstained=bool(data.get("abstained", False)),
        root_cause_service=data.get("root_cause_service"),
        root_cause_category=data.get("root_cause_category"),
        root_cause_statement=str(data.get("root_cause_statement", "")),
        affected_services=tuple(data.get("affected_services") or ()),
        causal_path=tuple(data.get("causal_path") or ()),
        confidence=float(data.get("confidence") or 0.0),
        cited_evidence_ids=tuple(data.get("cited_evidence_ids") or ()),
        cost=CostObservation(
            llm_cost_usd=float(cost.get("llm_usd") or 0.0),
            execution_cost_usd=float(cost.get("execution_usd") or 0.0),
            input_tokens=int(cost.get("input_tokens") or 0),
            output_tokens=int(cost.get("output_tokens") or 0),
            llm_calls=int(cost.get("llm_calls") or 0),
            tool_calls=int(cost.get("tool_calls") or 0),
            wall_ms=int(cost.get("wall_ms") or 0),
        ),
        harness_failure=harness_failure,
    )


def _evaluations_from_json(rows: Sequence[dict[str, Any]]) -> tuple[EvaluatorResult, ...]:
    out: list[EvaluatorResult] = []
    for row in rows:
        determinism = Determinism(row.get("determinism", Determinism.DETERMINISTIC.value))
        metrics = tuple(
            MetricValue(
                name=str(m["name"]),
                value=m.get("value"),
                unit=str(m.get("unit", "ratio")),
                sample_size=int(m.get("n", 1)),
                detail=dict(m.get("detail") or {}),
            )
            for m in row.get("metrics", [])
        )
        failures: list[FailureClass] = []
        for name in row.get("failure_classes", []):
            with contextlib.suppress(ValueError):
                failures.append(FailureClass(name))
        out.append(
            EvaluatorResult(
                evaluator=str(row.get("evaluator", "unknown")),
                version=str(row.get("version", "0")),
                determinism=determinism,
                metrics=metrics,
                failure_classes=tuple(failures),
                notes=tuple(row.get("notes", [])),
                judge_model=row.get("judge_model"),
                judge_prompt_version=row.get("judge_prompt_version"),
            )
        )
    return tuple(out)


def _result_from_row(scenario: Scenario, row: dict[str, Any]) -> ScenarioResult:
    """Rehydrate a persisted benchmark result."""
    failure_class: FailureClass | None = None
    raw_failure = row.get("failure_class")
    if raw_failure:
        with contextlib.suppress(ValueError):
            failure_class = FailureClass(str(raw_failure))
    recorded: datetime = row.get("created_at") or datetime.now(UTC)
    return ScenarioResult(
        scenario_id=scenario.id,
        scenario_hash=str(row.get("scenario_hash") or scenario.content_hash),
        category=str(row.get("category") or scenario.category.value),
        workload=scenario.workload.value,
        severity=scenario.severity.value,
        difficulty=scenario.difficulty.value,
        passed=bool(row.get("passed", False)),
        failure_class=failure_class,
        evaluations=_evaluations_from_json(list(row.get("evaluations") or [])),
        outcome=_outcome_from_predicted(dict(row.get("predicted") or {})),
        duration_ms=int(row.get("duration_ms") or 0),
        started_at=recorded,
        finished_at=recorded,
        ablation=str(row.get("ablation") or FULL),
        notes=("restored from a previous run",),
    )


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_SCENARIO_TIMEOUT_S",
    "MAX_RESUME_ROWS",
    "BenchmarkHarness",
    "HarnessConfig",
    "NullEnvironment",
    "PassCriteria",
    "ScenarioEnvironment",
    "SystemUnderTest",
    "classify_environment_error",
]
