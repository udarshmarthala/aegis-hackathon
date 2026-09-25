"""The evaluation surface, read back through the API.

The benchmark is the thing that says whether this build is better than the last
one, so the ways it can quietly lie are what these tests pin down:

* a harness failure must never be averaged into a quality metric, and must stay
  distinguishable from a model-quality failure in the response;
* the unsafe list must always be present, explicitly empty rather than omitted;
* a missing run must be a typed 404, not an empty object;
* the scenario catalogue must never carry ground truth, and an unreadable
  catalogue must not look like an empty one;
* ``safety_regressed`` must be decided on safety alone, whatever the pass rate
  did.

The database is a fake that answers the router's own queries; nothing here
needs Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aegis.api.deps import current_principal, db_dep, settings_dep
from aegis.api.routers import evaluation
from aegis.api.security import Principal, Role
from aegis.core.config import Settings
from aegis.core.errors import AegisError

STARTED = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _evaluation(evaluator: str, metrics: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    return {
        "evaluator": evaluator,
        "version": "1.0.0",
        "determinism": "deterministic",
        "metrics": metrics,
        "failure_classes": [],
        "notes": notes,
    }


def _result(
    scenario_id: str,
    *,
    category: str = "cache_failure",
    passed: bool = True,
    unsafe: bool = False,
    harness_failure: bool = False,
    failure_class: str | None = None,
    localization: float | None = 1.0,
    confidence: float = 0.9,
    abstained: bool = False,
    scenario_hash: str = "hash-a",
) -> dict[str, Any]:
    unsafe_value = 1.0 if unsafe else 0.0
    scores = {
        "root_cause_service_accuracy": localization,
        "unsafe_incident": unsafe_value,
    }
    return {
        "scenario_id": scenario_id,
        "scenario_hash": scenario_hash,
        "category": category,
        "ablation": "full",
        "incident_id": f"inc_{scenario_id}",
        "passed": passed,
        "unsafe": unsafe,
        "harness_failure": harness_failure,
        "failure_class": failure_class,
        "scores": scores,
        "evaluations": [
            _evaluation(
                "localization",
                [
                    {
                        "name": "root_cause_service_accuracy",
                        "value": localization,
                        "unit": "ratio",
                        "n": 1,
                    }
                ],
                [],
            ),
            _evaluation(
                "safety",
                [
                    {
                        "name": "unsafe_incident",
                        "value": unsafe_value,
                        "unit": "ratio",
                        "n": 1,
                    }
                ],
                ["executed a tier-2 action without approval"] if unsafe else [],
            ),
        ],
        "predicted": {"abstained": abstained, "confidence": confidence},
        "duration_ms": 1200,
        "tokens": 4000,
        "cost_usd": 0.02,
        "created_at": FINISHED,
    }


def _run(
    run_id: str,
    *,
    suite: str = "smoke",
    ablation: str = "full",
    status: str = "done",
    pass_rate: float = 0.5,
    unsafe: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "suite": suite,
        "ablation": ablation,
        "baseline": "aegis",
        "status": status,
        "agent_version": "2.0.0",
        "prompt_version": "1.0.0",
        "policy_version": "1.0.0",
        "model": "anthropic/claude-sonnet-4.5",
        "scenario_count": 3,
        "criteria_version": "1.0.0",
        "cost_model_version": "1.0.0",
        "summary": {
            "suite": suite,
            "ablation": ablation,
            "scenarios": 3,
            "scored": 2,
            "passed": 1,
            "pass_rate": pass_rate,
            "harness_failures": 1,
            "unsafe_scenarios": unsafe if unsafe is not None else [],
            "failure_classes": {"localization_failure": 1},
            "total_cost_usd": 0.06,
            "total_tokens": 12_000,
            "duration_s": 1800.0,
        },
        "started_at": STARTED,
        "finished_at": FINISHED,
    }


class FakeDB:
    """Answers the router's queries from in-memory rows, applying its filters."""

    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        results: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.runs = runs if runs is not None else [_run("run_a")]
        self.results = results if results is not None else {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM evaluation_runs" in query
        return next((r for r in self.runs if r["id"] == args[0]), None)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM evaluation_runs" in query:
            suite, ablation, status, limit = args
            rows = [
                r
                for r in self.runs
                if (suite is None or r["suite"] == suite)
                and (ablation is None or r["ablation"] == ablation)
                and (status is None or r["status"] == status)
            ]
            return rows[:limit]

        assert "FROM benchmark_results" in query
        rows = list(self.results.get(args[0], []))
        if "AND unsafe" in query:
            return [r for r in rows if r["unsafe"] and not r["harness_failure"]][: args[1]]
        if "OR category = $2" in query:
            category, only_failed, limit = args[1], args[2], args[3]
            if category is not None:
                rows = [r for r in rows if r["category"] == category]
            if only_failed:
                rows = [r for r in rows if not r["passed"]]
            return rows[:limit]
        return rows[: args[1]]


def build_app(db: FakeDB, *, settings: Settings | None = None, role: Role = Role.VIEWER) -> FastAPI:
    app = FastAPI()
    app.include_router(evaluation.router, prefix="/v1")

    @app.exception_handler(AegisError)
    async def _typed(request: Request, exc: AegisError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    async def _principal() -> Principal:
        return Principal(uid="uid_1", email="op@example.com", roles=frozenset({role.value}))

    app.dependency_overrides[db_dep] = lambda: db
    app.dependency_overrides[current_principal] = _principal
    if settings is not None:
        app.dependency_overrides[settings_dep] = lambda: settings
    return app


@pytest.fixture
def populated() -> FakeDB:
    return FakeDB(
        runs=[_run("run_a", unsafe=["CACHE-REDIS-004"])],
        results={
            "run_a": [
                _result("CACHE-REDIS-001"),
                _result(
                    "CACHE-REDIS-004",
                    passed=False,
                    unsafe=True,
                    failure_class="localization_failure",
                    localization=0.0,
                    confidence=0.8,
                ),
                _result(
                    "CACHE-REDIS-009",
                    passed=False,
                    harness_failure=True,
                    failure_class="environment_failure",
                    localization=0.0,
                    confidence=0.7,
                ),
            ]
        },
    )


# --------------------------------------------------------------------------- #
# runs                                                                         #
# --------------------------------------------------------------------------- #


def test_runs_are_listed_with_the_headline_the_harness_recorded(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs").json()

    assert body["count"] == 1
    run = body["items"][0]
    assert run["id"] == "run_a"
    assert run["headline"]["pass_rate"] == 0.5
    assert run["headline"]["harness_failures"] == 1
    assert run["headline"]["unsafe_count"] == 1


def test_run_filters_are_applied(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        assert client.get("/v1/evaluation/runs?suite=smoke").json()["count"] == 1
        assert client.get("/v1/evaluation/runs?suite=nightly").json()["count"] == 0


def test_a_missing_run_is_a_typed_404(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        response = client.get("/v1/evaluation/runs/run_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_harness_failures_are_excluded_from_quality_aggregates(populated: FakeDB) -> None:
    """A Prometheus outage is not a localization failure (ESD 32)."""
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a").json()

    localization = body["metrics"]["root_cause_service_accuracy"]
    # Two scored scenarios, one right and one wrong. The harness failure is not
    # a third zero.
    assert localization["n"] == 2
    assert localization["mean"] == 0.5
    assert localization["determinism"] == "deterministic"
    assert body["counts"]["harness_failures"] == 1
    assert body["counts"]["scored"] == 2


def test_category_breakdown_separates_scored_from_harness_failures(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a").json()

    category = body["categories"][0]
    assert category["total"] == 3
    assert category["scored"] == 2
    assert category["harness_failures"] == 1
    assert category["pass_rate"] == 0.5


# --------------------------------------------------------------------------- #
# scenarios                                                                    #
# --------------------------------------------------------------------------- #


def test_the_two_kinds_of_failure_stay_distinguishable(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a/scenarios").json()

    kinds = {item["scenario_id"]: item["failure_kind"] for item in body["items"]}
    assert kinds["CACHE-REDIS-001"] is None
    assert kinds["CACHE-REDIS-004"] == "model_quality"
    assert kinds["CACHE-REDIS-009"] == "harness"
    assert body["harness_failures"] == 1
    assert body["model_quality_failures"] == 1


def test_scenarios_can_be_narrowed_to_failures(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a/scenarios?only_failed=true").json()

    assert {i["scenario_id"] for i in body["items"]} == {"CACHE-REDIS-004", "CACHE-REDIS-009"}


# --------------------------------------------------------------------------- #
# unsafe                                                                       #
# --------------------------------------------------------------------------- #


def test_the_unsafe_list_carries_its_reasons(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a/unsafe").json()

    assert body["count"] == 1
    entry = body["unsafe"][0]
    assert entry["scenario_id"] == "CACHE-REDIS-004"
    assert entry["reasons"] == ["executed a tier-2 action without approval"]


def test_the_unsafe_list_is_present_and_empty_for_a_clean_run() -> None:
    """Never omitted. A section that vanishes when empty stops being read."""
    db = FakeDB(runs=[_run("run_clean")], results={"run_clean": [_result("CACHE-REDIS-001")]})

    with TestClient(build_app(db)) as client:
        body = client.get("/v1/evaluation/runs/run_clean/unsafe").json()

    assert body["unsafe"] == []
    assert body["count"] == 0


# --------------------------------------------------------------------------- #
# calibration                                                                  #
# --------------------------------------------------------------------------- #


def test_calibration_bins_cover_the_range_and_name_their_basis(populated: FakeDB) -> None:
    with TestClient(build_app(populated)) as client:
        body = client.get("/v1/evaluation/runs/run_a/calibration").json()

    assert len(body["bins"]) == 10
    assert body["bins"][0]["lower"] == 0.0
    assert body["bins"][-1]["upper"] == 1.0
    assert body["basis"] == "root_cause_service_accuracy"
    # Two scored, non-abstaining scenarios: one right at 0.9, one wrong at 0.8.
    assert body["scored"] == 2
    assert body["accuracy"] == 0.5
    assert body["brier_score"] == pytest.approx(((0.9 - 1.0) ** 2 + 0.8**2) / 2)
    assert body["excluded"]["harness_failure"] == 1


def test_abstentions_are_excluded_from_the_reliability_curve() -> None:
    """An abstention makes no probabilistic claim, so it calibrates nothing."""
    db = FakeDB(
        runs=[_run("run_b")],
        results={
            "run_b": [
                _result("CACHE-REDIS-001"),
                _result("AMBIG-SYMPT-002", abstained=True, passed=True, localization=None),
            ]
        },
    )

    with TestClient(build_app(db)) as client:
        body = client.get("/v1/evaluation/runs/run_b/calibration").json()

    assert body["scored"] == 1
    assert body["excluded"]["abstained"] == 1


# --------------------------------------------------------------------------- #
# catalogue                                                                    #
# --------------------------------------------------------------------------- #

_GROUND_TRUTH_KEYS = {
    "ground_truth",
    "fault",
    "description",
    "root_cause_service",
    "root_cause_category",
    "causal_dependency",
    "expected_remediation_category",
}


def test_the_catalogue_exposes_metadata_and_never_ground_truth() -> None:
    db = FakeDB()
    with TestClient(build_app(db)) as client:
        body = client.get("/v1/evaluation/scenarios?limit=5").json()

    assert body["available"] is True
    assert body["items"], "the repository ships a scenario catalogue"
    for item in body["items"]:
        assert set(item) & _GROUND_TRUTH_KEYS == set()
        assert item["id"] and item["title"]
        assert item["category"] and item["workload"] and item["difficulty"]


def test_an_unreadable_catalogue_is_not_an_empty_catalogue(tmp_path: Any) -> None:
    """"We could not look" and "there is nothing there" are different answers."""
    settings = Settings(
        postgres_password="x", eval_scenarios_dir=str(tmp_path / "does-not-exist")
    )

    with TestClient(build_app(FakeDB(), settings=settings)) as client:
        body = client.get("/v1/evaluation/scenarios").json()

    assert body["available"] is False
    assert body["reason"]
    assert body["items"] == []


# --------------------------------------------------------------------------- #
# comparison                                                                   #
# --------------------------------------------------------------------------- #


def test_a_new_unsafe_scenario_regresses_safety_whatever_the_pass_rate_did() -> None:
    """An aggregate improvement never licenses a safety regression (PRD 9.3)."""
    db = FakeDB(
        runs=[_run("run_base", pass_rate=0.5), _run("run_candidate", pass_rate=1.0)],
        results={
            "run_base": [_result("CACHE-REDIS-001"), _result("CACHE-REDIS-004", localization=0.0)],
            "run_candidate": [
                _result("CACHE-REDIS-001"),
                _result("CACHE-REDIS-004", unsafe=True, passed=False, localization=1.0),
            ],
        },
    )

    with TestClient(build_app(db)) as client:
        body = client.get(
            "/v1/evaluation/compare?base=run_base&candidate=run_candidate"
        ).json()

    assert body["safety_regressed"] is True
    assert body["new_unsafe_scenarios"] == ["CACHE-REDIS-004"]
    assert body["pass_rate"]["delta"] == 0.5
    names = {d["name"]: d for d in body["metrics"]}
    assert names["root_cause_service_accuracy"]["delta"] == 0.5


def test_a_clean_comparison_reports_no_safety_regression() -> None:
    db = FakeDB(
        runs=[_run("run_base"), _run("run_candidate")],
        results={
            "run_base": [_result("CACHE-REDIS-001", localization=0.0, passed=False)],
            "run_candidate": [_result("CACHE-REDIS-001")],
        },
    )

    with TestClient(build_app(db)) as client:
        body = client.get(
            "/v1/evaluation/compare?base=run_base&candidate=run_candidate"
        ).json()

    assert body["safety_regressed"] is False
    assert body["new_unsafe_scenarios"] == []
    assert body["changed_scenarios"] == []


def test_a_scenario_edited_between_runs_is_flagged_not_averaged() -> None:
    db = FakeDB(
        runs=[_run("run_base"), _run("run_candidate")],
        results={
            "run_base": [_result("CACHE-REDIS-001", scenario_hash="hash-a")],
            "run_candidate": [_result("CACHE-REDIS-001", scenario_hash="hash-b")],
        },
    )

    with TestClient(build_app(db)) as client:
        body = client.get(
            "/v1/evaluation/compare?base=run_base&candidate=run_candidate"
        ).json()

    assert body["changed_scenarios"] == ["CACHE-REDIS-001"]


def test_comparing_against_a_missing_run_is_a_404() -> None:
    db = FakeDB(runs=[_run("run_base")], results={"run_base": [_result("CACHE-REDIS-001")]})

    with TestClient(build_app(db)) as client:
        response = client.get("/v1/evaluation/compare?base=run_base&candidate=run_gone")

    assert response.status_code == 404
