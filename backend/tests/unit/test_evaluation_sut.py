"""The benchmark's edges: what gets injected, and what gets observed.

These cover the two places where a benchmark number can be true-looking and
false. Both were live defects:

* ``WorkloadFaultInjector`` implements three of the seventeen declared fault
  modes. It used to log a warning and inject nothing, after which the harness
  scored a healthy workload against an answer key describing a fault nobody had
  applied. Strict is now the default, and the preflight says up front how much
  of a suite this environment can actually run.
* ``AegisSystemUnderTest`` reported ``detected=True`` unconditionally, which
  made ``DETECTION_FAILURE`` unreachable, and read a verification verdict from
  a state key nothing ever wrote, which made every verification metric
  permanently ``None``.

``eval/`` is a deliberately separate tree - the injector must not be able to
import the agent - so it is added to ``sys.path`` here the same way ``run.py``
adds it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from aegis.agents.state import BudgetGuard
from aegis.agents.workflow import WorkflowDeps, make_nodes
from aegis.core.config import Settings
from aegis.core.errors import ExternalServiceError
from aegis.domain.enums import (
    ActionState,
    ActionType,
    FailureClass,
    Severity,
    VerificationVerdict,
)
from aegis.evaluation.harness import classify_environment_error
from aegis.evaluation.schema import (
    AlertSpec,
    FaultInjection,
    FaultMode,
    GroundTruth,
    Scenario,
    ScenarioCategory,
    SecondaryFault,
    Workload,
)
from aegis.execution.service import ExecutionReport
from aegis.verification.engine import Baseline, VerificationRun

EVAL_DIR = Path(__file__).resolve().parents[3] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import aegis_sut  # noqa: E402
import run as eval_run  # noqa: E402
from injector import WorkloadFaultInjector  # noqa: E402


def scenario(
    sid: str = "TST-REF-001",
    *,
    mode: FaultMode = FaultMode.LATENCY,
    secondary: tuple[SecondaryFault, ...] = (),
) -> Scenario:
    return Scenario(
        id=sid,
        title="Gateway latency rises because payment slowed",
        category=ScenarioCategory.LATENCY,
        description="A synthetic scenario used by the injector tests.",
        workload=Workload.REFERENCE,
        severity=Severity.P2,
        fault=FaultInjection(
            target="payment",
            mode=mode,
            magnitude_ms=300 if mode is FaultMode.LATENCY else 0,
            secondary=secondary,
        ),
        alert=AlertSpec(title="Gateway p99 latency above budget", severity=Severity.P2,
                        fires_after_s=0),
        ground_truth=GroundTruth(affected_services=("gateway",),
                                 root_cause_service="payment",
                                 root_cause_category="upstream_latency"),
    )


# --------------------------------------------------------------------------- #
# injection: what this environment can and cannot apply                        #
# --------------------------------------------------------------------------- #


def without_mode(mode: FaultMode) -> Any:
    """Pretend one mode is unimplemented, so the refusal path stays testable.

    The workload implements all seventeen modes now, so the branch that refuses
    an uninjectable one no longer fires in this environment. That branch is a
    safety property - it is what stops a benchmark scoring a healthy system
    against a fault nobody applied - so it is still exercised, against a
    deliberately narrowed capability set rather than against a real gap.
    """
    return mock.patch.object(
        WorkloadFaultInjector,
        "supported_modes",
        staticmethod(lambda: frozenset(FaultMode) - {mode}),
    )


async def test_an_uninjectable_fault_mode_refuses_by_default() -> None:
    """The default must be the safe one. A benchmark run by someone who read no
    flags has to be a benchmark that refuses to score an un-injected scenario."""
    injector = WorkloadFaultInjector(settle=False)

    with without_mode(FaultMode.PROCESS_KILL), pytest.raises(ExternalServiceError) as raised:
        await injector.prepare(scenario(mode=FaultMode.PROCESS_KILL))

    assert "runtime-level injector" in raised.value.message
    assert raised.value.context["reason"] == "uninjectable_fault_mode"

    # And the harness reads that refusal as an environment problem, so the
    # scenario is excluded from quality aggregates rather than blamed on a model.
    failure = classify_environment_error(raised.value)
    assert failure is not None
    assert failure.failure_class is FailureClass.ENVIRONMENT_FAILURE
    assert failure.failure_class.is_harness_failure is True


async def test_the_escape_hatch_is_explicit_and_injects_nothing() -> None:
    """strict=False is for someone who deliberately wants the partial subset. It
    still makes no HTTP call, so it cannot half-inject a scenario either."""
    injector = WorkloadFaultInjector(endpoints={"payment": "http://127.0.0.1:1"},
                                     settle=False, strict=False)
    with without_mode(FaultMode.CPU_BURN):
        await injector.prepare(scenario(mode=FaultMode.CPU_BURN))
    await injector.close()


def test_supported_modes_is_the_only_list_of_injectable_modes() -> None:
    """Every declared mode is implemented, and the list has exactly one home.

    The workload used to implement three of seventeen, so most of the corpus was
    unrunnable. It now implements all of them. What is still worth pinning is the
    single-source-of-truth property: a preflight that kept its own copy of this
    list would go stale the first time the workload learned a mode, and the
    symptom would be a scenario reported as unrunnable that runs perfectly well.
    """
    supported = WorkloadFaultInjector.supported_modes()

    assert supported == frozenset(FaultMode)
    assert FaultMode.NONE in supported          # false positives inject nothing
    # Modes that needed a runtime-level mechanism and now have a real one.
    assert {FaultMode.PROCESS_KILL, FaultMode.CPU_BURN, FaultMode.DNS_FAILURE} <= supported


def test_a_secondary_fault_is_checked_as_well_as_the_primary() -> None:
    """Half a fault is not the scenario the answer key describes.

    Every mode is injectable now, so this asserts the mechanism rather than a
    gap: a secondary fault goes through exactly the same check as the primary,
    and reintroducing a mode the workload cannot apply would block the scenario
    instead of half-injecting it.
    """
    both = scenario(
        mode=FaultMode.LATENCY,
        secondary=(SecondaryFault(target="cart", mode=FaultMode.PROCESS_KILL),),
    )
    assert WorkloadFaultInjector.unsupported_modes(both) == ()
    assert WorkloadFaultInjector.can_inject(both) is True

    # The secondary really is inspected, not skipped: a mode absent from the
    # supported set blocks the scenario even when the primary is fine.
    pretend_unsupported = frozenset(FaultMode) - {FaultMode.PROCESS_KILL}
    with mock.patch.object(
        WorkloadFaultInjector, "supported_modes", staticmethod(lambda: pretend_unsupported)
    ):
        assert WorkloadFaultInjector.unsupported_modes(both) == (FaultMode.PROCESS_KILL,)
        assert WorkloadFaultInjector.can_inject(both) is False


def test_the_preflight_partitions_a_suite_before_anything_runs() -> None:
    """Both reasons a scenario cannot run are checked, not just the mode.

    Checking only the fault mode is how a preflight came to report twenty-one
    runnable scenarios on a machine where thirteen could run: the rest passed the
    mode check while naming a service that resolved to nothing, and the run
    discovered it one connection timeout at a time.
    """
    selected = [
        scenario("TST-REF-001", mode=FaultMode.LATENCY),
        scenario("TST-REF-002", mode=FaultMode.CPU_BURN),
        scenario("TST-REF-003", mode=FaultMode.NONE),
    ]

    class _KnowsNothing:
        """An environment where no target resolves."""

        @staticmethod
        def knows(target: str, workload: str = "") -> bool:
            return False

    injectable, blocked = eval_run.partition_injectable(
        selected, WorkloadFaultInjector, _KnowsNothing()
    )

    # All three name a target, so with nothing resolvable all three are blocked -
    # and the reason travels with the scenario, because "which ones and why" is
    # what an operator needs before waiting out a run, not a count.
    assert injectable == []
    assert [s.id for s, _ in blocked] == ["TST-REF-001", "TST-REF-002", "TST-REF-003"]
    assert all(isinstance(r, str) for _, reasons in blocked for r in reasons)

    # With a resolving environment and every mode implemented, nothing is blocked.
    class _KnowsEverything:
        @staticmethod
        def knows(target: str, workload: str = "") -> bool:
            return True

    injectable, blocked = eval_run.partition_injectable(
        selected, WorkloadFaultInjector, _KnowsEverything()
    )
    assert len(injectable) == 3
    assert blocked == []


def test_allow_uninjectable_is_not_the_default() -> None:
    args = eval_run.build_parser().parse_args([])
    assert args.allow_uninjectable is False
    assert eval_run.build_parser().parse_args(["--allow-uninjectable"]).allow_uninjectable


# --------------------------------------------------------------------------- #
# observation: detection is derived, never assumed                             #
# --------------------------------------------------------------------------- #


def test_detection_is_derived_from_the_phase_the_run_reached() -> None:
    reached = aegis_sut._detection_observed("inc_1", {"phase": "MONITORING"})
    assert reached is True
    assert aegis_sut._detection_observed("inc_1", {"phase": "DIAGNOSING"}) is True
    assert aegis_sut._detection_observed("inc_1", {"phase": "ESCALATED"}) is True


def test_a_run_that_never_started_investigating_did_not_detect() -> None:
    """This is the case that used to be reported as a success, which made
    DETECTION_FAILURE unreachable and every detection number a tautology."""
    assert aegis_sut._detection_observed("inc_1", {"phase": "RECEIVED"}) is False
    assert aegis_sut._detection_observed("inc_1", {"phase": "TRIAGING"}) is False
    assert aegis_sut._detection_observed("", {"phase": "MONITORING"}) is False


def test_unobservable_detection_is_unknown_rather_than_true() -> None:
    assert aegis_sut._detection_observed("inc_1", {}) is None
    assert aegis_sut._detection_observed("inc_1", {"phase": ""}) is None
    assert aegis_sut._detection_observed("inc_1", {"phase": "NOT_A_PHASE"}) is None


def test_an_unverified_arm_is_distinguishable_from_a_verified_one() -> None:
    assert aegis_sut._grounding_verified({"evidence_verification": "validated"}) is True
    assert aegis_sut._grounding_verified({"evidence_verification": "skipped"}) is False
    assert aegis_sut._grounding_verified({}) is None


# --------------------------------------------------------------------------- #
# observation: the verification verdict reaches the state the SUT reads        #
# --------------------------------------------------------------------------- #


class FakeDB:
    async def execute(self, query: str, *args: Any) -> str:
        del query, args
        return "INSERT 0 1"


class OneAction:
    """Stands in for a ValidatedAction, which only ``ActionGate`` may build."""

    action_type = ActionType.RESTART_INSTANCE


def execution_report(verdict: VerificationVerdict) -> ExecutionReport:
    started = datetime.now(UTC)
    return ExecutionReport(
        action_id="act_1",
        incident_id="inc_1",
        executed=True,
        outcome=None,
        verification=VerificationRun(
            id="ver_1",
            incident_id="inc_1",
            action_id="act_1",
            kind="post_action",
            verdict=verdict,
            results=[],
            baseline=Baseline(captured_at=started, window_start=0.0, window_end=1.0),
            started_at=started,
            completed_at=started + timedelta(seconds=30),
            notes="",
        ),
        rolled_back=False,
        rollback_outcome=None,
        final_state=ActionState.SUCCESS,
        escalated=False,
        escalation_reason="",
        started_at=started,
        finished_at=started + timedelta(seconds=45),
    )


class FakeExecutor:
    def __init__(self, report: ExecutionReport) -> None:
        self.report = report

    async def execute(self, validated: Any, ports: Any) -> ExecutionReport:
        del validated, ports
        return self.report


def deps_with(executor: Any) -> WorkflowDeps:
    deps = WorkflowDeps(
        settings=Settings(postgres_password="x"),
        db=FakeDB(),
        evidence=object(),
        prometheus=object(),
        neo4j=object(),
        router=object(),
        budget=BudgetGuard(max_wall_seconds=60, max_llm_calls=5,
                           max_tool_calls=5, max_tokens=1000),
        executor=executor,
        ports=object(),
    )
    deps.pending_action = OneAction()
    return deps


async def test_the_verification_verdict_reaches_the_key_the_sut_reads() -> None:
    """The defect: the execution node produced a verdict and left it inside the
    remediation blob, while the system under test read ``verification_verdict``
    off the state. Nothing wrote that key, so verification success, rollback
    correctness and regression rate were permanently ``None`` in live runs."""
    report = execution_report(VerificationVerdict.VERIFIED)
    nodes = make_nodes(deps_with(FakeExecutor(report)))

    state = await nodes["execute_remediation"]({"incident_id": "inc_1"})

    assert state["verification_verdict"] == "VERIFIED"
    # ... and the reader on the other side of the boundary resolves it.
    assert aegis_sut._verdict_of(state) is VerificationVerdict.VERIFIED


async def test_a_regression_verdict_is_carried_through_unchanged() -> None:
    report = execution_report(VerificationVerdict.REGRESSION_DETECTED)
    nodes = make_nodes(deps_with(FakeExecutor(report)))

    state = await nodes["execute_remediation"]({"incident_id": "inc_1"})

    assert aegis_sut._verdict_of(state) is VerificationVerdict.REGRESSION_DETECTED


async def test_no_execution_means_no_verdict_rather_than_a_default() -> None:
    """"Not measured" and "measured as failed" are different facts, and only one
    of them belongs in a report."""
    deps = deps_with(None)
    deps.pending_action = None
    nodes = make_nodes(deps)

    state = await nodes["execute_remediation"]({"incident_id": "inc_1"})

    assert "verification_verdict" not in state
    assert aegis_sut._verdict_of(state) is None
