"""Evaluation: what the benchmark says about this build.

Everything here reads the rows the harness already wrote (``evaluation_runs``
and ``benchmark_results``). Nothing re-runs a scenario, and nothing recomputes a
pass/fail verdict - the harness decided those, deterministically, and an API
that re-derived them could disagree with the release gate that used them.

Four properties carry the weight of this module.

**A harness failure is not a model-quality failure.** ESD 32 keeps them apart
and so does every response here: ``harness_failure`` stays on each scenario row,
``failure_kind`` names which of the two it was, and every aggregate is taken
over scored scenarios only. Averaging a Prometheus outage into a localization
score is how a benchmark quietly starts lying.

**The unsafe list is always present.** ``/unsafe`` returns an explicit empty
array rather than omitting the key. A section that disappears when it is empty
trains readers to skim past it, and the one time it matters is the one time
nobody looks.

**Ground truth never leaves the process.** The catalogue endpoint serialises
scenario *metadata* only - id, title, category, workload, difficulty - built
from a fixed field list that does not include ``ground_truth`` or ``fault``.
``evaluation.schema.assert_sealed`` guards what a *system under test* may see;
this is an operator surface, and it still never emits the answer key.

**"No runs" and "no catalogue" are different answers.** An empty run list means
nothing has been benchmarked. An unreadable scenario directory is reported as
``available: false`` with a reason, because reporting it as an empty catalogue
would say the suite has no scenarios (PRD 13).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Final, TypeGuard

from fastapi import APIRouter, Query

from aegis.api.deps import DbDep, RequireViewer, SettingsDep
from aegis.core.config import Settings
from aegis.core.errors import NotFoundError
from aegis.core.logging import get_logger
from aegis.evaluation.evaluators.base import Determinism
from aegis.evaluation.evaluators.calibration import (
    DEFAULT_BINS,
    brier_score,
    calibration_error,
    reliability_bins,
)
from aegis.evaluation.report import ReportDigest, compare
from aegis.evaluation.results import bootstrap_ci
from aegis.evaluation.schema import ScenarioInvalid, load_scenarios
from aegis.persistence.db import Database

log = get_logger(__name__)
router = APIRouter(prefix="/evaluation", tags=["evaluation"])

# Every query below is bounded. A benchmark suite is a few hundred scenarios at
# most, so these caps never truncate a real run - they exist so that a corrupt
# or adversarial row count cannot turn one request into an unbounded read.
MAX_RUNS: Final = 100
MAX_RESULT_ROWS: Final = 1_000

# The metric a stated confidence is scored against when the reliability diagram
# is rebuilt from stored rows. See ``_calibration_pairs`` for why this is the
# one available basis and how it differs from the harness's own run-level
# calibration evaluator.
CONFIDENCE_TARGET_METRIC: Final = "root_cause_service_accuracy"

# ``eval_scenarios_dir`` is usually relative. Resolving it against the checkout
# root as well as the working directory means the same configuration works
# whether the API runs from ``backend/`` inside a container or from the repo in
# a test, without either one needing an absolute path.
_REPO_ROOT: Final = Path(__file__).resolve().parents[5]


# --------------------------------------------------------------------------- #
# shared readers                                                               #
# --------------------------------------------------------------------------- #


async def _require_run(db: Database, run_id: str) -> dict[str, Any]:
    """One run, or a typed 404. Never ``None`` for a caller to misread."""
    row = await db.fetchrow(
        """
        SELECT id, suite, ablation, baseline, status, agent_version, prompt_version,
               policy_version, model, scenario_count, criteria_version,
               cost_model_version, summary, started_at, finished_at
          FROM evaluation_runs
         WHERE id = $1
        """,
        run_id,
    )
    if row is None:
        raise NotFoundError(
            f"no evaluation run {run_id!r}", context={"run_id": run_id}
        )
    return dict(row)


async def _results_for(db: Database, run_id: str, *, limit: int) -> list[dict[str, Any]]:
    """Every scored scenario row for one run, ordered and bounded."""
    rows = await db.fetch(
        """
        SELECT scenario_id, scenario_hash, category, ablation, incident_id, passed,
               unsafe, harness_failure, failure_class, scores, evaluations,
               predicted, duration_ms, tokens, cost_usd, created_at
          FROM benchmark_results
         WHERE evaluation_run_id = $1
         ORDER BY scenario_id
         LIMIT $2
        """,
        run_id,
        limit,
    )
    return [dict(r) for r in rows]


def _headline(summary: dict[str, Any]) -> dict[str, Any]:
    """The numbers a run list shows, taken from what the harness recorded.

    Read straight off ``evaluation_runs.summary`` rather than recomputed, so the
    list and the harness's own report can never disagree about a pass rate.
    """
    unsafe = summary.get("unsafe_scenarios")
    return {
        "scenarios": summary.get("scenarios"),
        "scored": summary.get("scored"),
        "passed": summary.get("passed"),
        "pass_rate": summary.get("pass_rate"),
        "harness_failures": summary.get("harness_failures"),
        "unsafe_count": len(unsafe) if isinstance(unsafe, list) else 0,
        "failure_classes": summary.get("failure_classes") or {},
        "total_cost_usd": summary.get("total_cost_usd"),
        "total_tokens": summary.get("total_tokens"),
        "duration_s": summary.get("duration_s"),
    }


def _run_view(row: dict[str, Any]) -> dict[str, Any]:
    summary = row.get("summary") or {}
    started = row["started_at"]
    finished = row["finished_at"]
    return {
        "id": row["id"],
        "suite": row["suite"],
        "ablation": row["ablation"],
        "baseline": row["baseline"],
        "status": row["status"],
        "agent_version": row["agent_version"],
        "prompt_version": row["prompt_version"],
        "policy_version": row["policy_version"],
        "model": row["model"],
        "criteria_version": row["criteria_version"],
        "cost_model_version": row["cost_model_version"],
        "scenario_count": int(row["scenario_count"] or 0),
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "headline": _headline(dict(summary)),
    }


def _is_number(value: Any) -> TypeGuard[int | float]:
    """Reject ``bool``: it is an ``int`` in Python and never a metric value."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _metric_aggregates(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mean per metric over scored scenarios, with the harness's own rules.

    Harness failures are excluded, inapplicable values (``None``) are excluded
    rather than zeroed, and every aggregate carries its sample size, its
    bootstrap interval and how the number was produced. "0.81 over 4 scenarios"
    and "0.81 over 120" are different claims and both are reported as such.

    The run-level calibration metrics are absent here on purpose: they are
    computed over a whole run rather than per scenario, so re-averaging them
    would be arithmetic on the wrong population. ``/calibration`` rebuilds them.
    """
    values: dict[str, list[float]] = {}
    determinism: dict[str, str] = {}
    units: dict[str, str] = {}

    for row in rows:
        if row["harness_failure"]:
            continue
        for entry in row.get("evaluations") or []:
            if not isinstance(entry, dict):
                continue
            how = str(entry.get("determinism") or Determinism.DETERMINISTIC.value)
            for metric in entry.get("metrics") or []:
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("name") or "")
                if not name:
                    continue
                determinism[name] = how
                units[name] = str(metric.get("unit") or "ratio")
                value = metric.get("value")
                if _is_number(value):
                    values.setdefault(name, []).append(float(value))

    out: dict[str, dict[str, Any]] = {}
    for name in sorted(set(values) | set(determinism)):
        applicable = values.get(name, [])
        low, high = bootstrap_ci(applicable) if applicable else (None, None)
        out[name] = {
            "name": name,
            "mean": round(sum(applicable) / len(applicable), 6) if applicable else None,
            "n": len(applicable),
            "ci_low": low,
            "ci_high": high,
            "determinism": determinism.get(name, Determinism.DETERMINISTIC.value),
            "unit": units.get(name, "ratio"),
        }
    return out


def _category_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-category totals. A regression hidden inside one category is exactly
    what an aggregate pass rate conceals (PRD 9.3)."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row["category"] or "uncategorised"), []).append(row)

    out: list[dict[str, Any]] = []
    for category, group in sorted(buckets.items()):
        harness = sum(1 for r in group if r["harness_failure"])
        scored = len(group) - harness
        passed = sum(1 for r in group if r["passed"] and not r["harness_failure"])
        out.append(
            {
                "category": category,
                "total": len(group),
                "scored": scored,
                "passed": passed,
                "pass_rate": (passed / scored) if scored > 0 else None,
                "harness_failures": harness,
                "unsafe": sum(1 for r in group if r["unsafe"]),
            }
        )
    return out


def _failure_kind(row: dict[str, Any]) -> str | None:
    """Which kind of failure this was, if it was one at all.

    Three distinct states, never collapsed: the scenario passed, the harness
    could not run it, or the system under test got it wrong.
    """
    if row["harness_failure"]:
        return "harness"
    if not row["passed"]:
        return "model_quality"
    return None


def _safety_notes(row: dict[str, Any]) -> list[str]:
    """Why the safety evaluator flagged this scenario, in its own words."""
    for entry in row.get("evaluations") or []:
        if isinstance(entry, dict) and entry.get("evaluator") == "safety":
            notes = entry.get("notes") or []
            return [str(n) for n in notes if isinstance(n, str)]
    return []


def _scenario_view(row: dict[str, Any]) -> dict[str, Any]:
    predicted = row.get("predicted") or {}
    created = row["created_at"]
    return {
        "scenario_id": row["scenario_id"],
        "scenario_hash": row["scenario_hash"],
        "category": row["category"],
        "ablation": row["ablation"],
        "incident_id": row["incident_id"],
        "passed": bool(row["passed"]),
        "unsafe": bool(row["unsafe"]),
        # Both are emitted. The boolean is what a filter uses; the kind is what
        # a reader needs so "failed" is never read as "the model was wrong".
        "harness_failure": bool(row["harness_failure"]),
        "failure_kind": _failure_kind(row),
        "failure_class": row["failure_class"],
        "abstained": bool(predicted.get("abstained", False)),
        "confidence": predicted.get("confidence"),
        "duration_ms": int(row["duration_ms"] or 0),
        "tokens": int(row["tokens"] or 0),
        "cost_usd": float(row["cost_usd"] or 0),
        "metrics": row.get("scores") or {},
        "created_at": created.isoformat() if created else None,
    }


# --------------------------------------------------------------------------- #
# runs                                                                         #
# --------------------------------------------------------------------------- #


@router.get("/runs", summary="List evaluation runs")
async def list_runs(
    _: RequireViewer,
    db: DbDep,
    suite: Annotated[str | None, Query(max_length=64)] = None,
    ablation: Annotated[str | None, Query(max_length=64)] = None,
    status: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 25,
) -> dict[str, Any]:
    """Recent runs, newest first, with the headline numbers each recorded.

    Filters are bound parameters rather than interpolated SQL, and a ``NULL``
    filter matches everything - so an omitted filter and an empty one behave
    identically instead of one of them silently matching nothing.
    """
    rows = await db.fetch(
        """
        SELECT id, suite, ablation, baseline, status, agent_version, prompt_version,
               policy_version, model, scenario_count, criteria_version,
               cost_model_version, summary, started_at, finished_at
          FROM evaluation_runs
         WHERE ($1::text IS NULL OR suite = $1)
           AND ($2::text IS NULL OR ablation = $2)
           AND ($3::text IS NULL OR status = $3)
         ORDER BY started_at DESC, id DESC
         LIMIT $4
        """,
        suite,
        ablation,
        status,
        limit,
    )
    items = [_run_view(dict(r)) for r in rows]
    return {"items": items, "count": len(items), "limit": limit}


@router.get("/runs/{run_id}", summary="One evaluation run with its aggregates")
async def get_run(run_id: str, _: RequireViewer, db: DbDep) -> dict[str, Any]:
    """The run, its per-category breakdown and its per-metric aggregates."""
    run = await _require_run(db, run_id)
    rows = await _results_for(db, run_id, limit=MAX_RESULT_ROWS)
    scored = [r for r in rows if not r["harness_failure"]]

    return {
        **_run_view(run),
        "counts": {
            "results": len(rows),
            "scored": len(scored),
            "harness_failures": len(rows) - len(scored),
            "unsafe": sum(1 for r in scored if r["unsafe"]),
            "truncated": len(rows) >= MAX_RESULT_ROWS,
        },
        "categories": _category_breakdown(rows),
        "metrics": _metric_aggregates(rows),
    }


@router.get("/runs/{run_id}/scenarios", summary="Per-scenario results for a run")
async def run_scenarios(
    run_id: str,
    _: RequireViewer,
    db: DbDep,
    category: Annotated[str | None, Query(max_length=64)] = None,
    only_failed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_RESULT_ROWS)] = 200,
) -> dict[str, Any]:
    """Every scenario in the run, with its failure class and failure *kind*.

    A scenario that the harness could not run and a scenario the system got
    wrong are both "not passed" and are never the same thing. Both fields are
    returned so a caller cannot conflate them by accident.
    """
    await _require_run(db, run_id)
    rows = await db.fetch(
        """
        SELECT scenario_id, scenario_hash, category, ablation, incident_id, passed,
               unsafe, harness_failure, failure_class, scores, evaluations,
               predicted, duration_ms, tokens, cost_usd, created_at
          FROM benchmark_results
         WHERE evaluation_run_id = $1
           AND ($2::text IS NULL OR category = $2)
           AND ($3::boolean IS FALSE OR NOT passed)
         ORDER BY scenario_id
         LIMIT $4
        """,
        run_id,
        category,
        only_failed,
        limit,
    )
    items = [_scenario_view(dict(r)) for r in rows]
    return {
        "run_id": run_id,
        "items": items,
        "count": len(items),
        "harness_failures": sum(1 for i in items if i["failure_kind"] == "harness"),
        "model_quality_failures": sum(
            1 for i in items if i["failure_kind"] == "model_quality"
        ),
        "truncated": len(items) >= limit,
    }


@router.get("/runs/{run_id}/unsafe", summary="Unsafe scenarios in a run")
async def run_unsafe(run_id: str, _: RequireViewer, db: DbDep) -> dict[str, Any]:
    """The unsafe list, always present - explicitly empty when there are none.

    Scoped to scored scenarios, because an environment failure is not the system
    behaving unsafely, and counting it as one would make a broken Prometheus
    look like a safety regression.
    """
    await _require_run(db, run_id)
    rows = await db.fetch(
        """
        SELECT scenario_id, category, failure_class, evaluations, incident_id
          FROM benchmark_results
         WHERE evaluation_run_id = $1
           AND unsafe
           AND NOT harness_failure
         ORDER BY scenario_id
         LIMIT $2
        """,
        run_id,
        MAX_RESULT_ROWS,
    )
    items = [
        {
            "scenario_id": r["scenario_id"],
            "category": r["category"],
            "failure_class": r["failure_class"],
            "incident_id": r["incident_id"],
            "reasons": _safety_notes(dict(r)),
        }
        for r in rows
    ]
    return {"run_id": run_id, "unsafe": items, "count": len(items)}


# --------------------------------------------------------------------------- #
# calibration                                                                  #
# --------------------------------------------------------------------------- #


def _calibration_pairs(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[float, bool]], dict[str, int]]:
    """(confidence, was-it-right) pairs, plus why each excluded row was excluded.

    Three exclusions, each matching ``CalibrationEvaluator``:

    * a harness failure is the environment being wrong, not the confidence;
    * an abstention makes no probabilistic claim, so it has nothing to calibrate;
    * a scenario with no applicable root-cause metric (a false-positive alert,
      say) has no binary event for the confidence to be a prediction about.

    The correctness basis is ``root_cause_service_accuracy`` as the harness
    stored it. That is narrower than the evaluator's own rule, which also
    requires the root-cause *category* to match - the category comparison needs
    the answer key, and the answer key deliberately never reaches this process
    through the API. The response names the basis so the number is never read as
    something it is not.
    """
    pairs: list[tuple[float, bool]] = []
    excluded = {"harness_failure": 0, "abstained": 0, "not_applicable": 0}

    for row in rows:
        if row["harness_failure"]:
            excluded["harness_failure"] += 1
            continue
        predicted = row.get("predicted") or {}
        if predicted.get("abstained"):
            excluded["abstained"] += 1
            continue
        scores = row.get("scores") or {}
        accuracy = scores.get(CONFIDENCE_TARGET_METRIC)
        confidence = predicted.get("confidence")
        if not _is_number(accuracy) or not _is_number(confidence):
            excluded["not_applicable"] += 1
            continue
        pairs.append((float(confidence), float(accuracy) >= 1.0))

    return pairs, excluded


@router.get("/runs/{run_id}/calibration", summary="Reliability diagram for a run")
async def run_calibration(
    run_id: str,
    _: RequireViewer,
    db: DbDep,
    bins: Annotated[int, Query(ge=2, le=20)] = DEFAULT_BINS,
) -> dict[str, Any]:
    """Reliability bins with Brier score and calibration error.

    Rebuilt from the stored per-scenario rows with the same functions the
    harness uses, so the chart and the report cannot drift apart in their
    arithmetic. Empty bins are returned as bins with ``count: 0`` rather than
    dropped - a gap in the diagram is information.
    """
    await _require_run(db, run_id)
    rows = await _results_for(db, run_id, limit=MAX_RESULT_ROWS)
    pairs, excluded = _calibration_pairs(rows)
    expected, worst = calibration_error(pairs, bins)

    return {
        "run_id": run_id,
        "basis": CONFIDENCE_TARGET_METRIC,
        "bins": [b.as_json() for b in reliability_bins(pairs, bins)],
        "brier_score": brier_score(pairs),
        "expected_calibration_error": expected,
        "max_calibration_error": worst,
        "mean_confidence": (
            round(sum(c for c, _ in pairs) / len(pairs), 6) if pairs else None
        ),
        "accuracy": (
            round(sum(1 for _, ok in pairs if ok) / len(pairs), 6) if pairs else None
        ),
        "scored": len(pairs),
        "excluded": excluded,
    }


# --------------------------------------------------------------------------- #
# catalogue                                                                    #
# --------------------------------------------------------------------------- #


def _scenario_root(settings: Settings) -> Path:
    configured = Path(settings.eval_scenarios_dir)
    if configured.is_absolute():
        return configured
    for base in (Path.cwd(), _REPO_ROOT):
        candidate = base / configured
        if candidate.is_dir():
            return candidate
    return Path.cwd() / configured


@router.get("/scenarios", summary="The benchmark catalogue")
async def scenario_catalogue(
    _: RequireViewer,
    settings: SettingsDep,
    category: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Scenario metadata for the benchmark page. No ground truth, ever.

    The payload is built from an explicit field list. ``ground_truth``,
    ``fault`` and ``description`` are not in it, and adding one would be a
    visible edit here rather than a field that arrives by serialising the whole
    model. This is an operator surface: a system under test never reads it, and
    only ever receives ``Scenario.to_input()``, which ``assert_sealed`` checks.

    Loading walks a directory, so it runs in a worker thread - a filesystem
    stall must not block the event loop for every other request.
    """
    root = _scenario_root(settings)
    bounded = min(limit, settings.eval_catalogue_limit)
    try:
        scenarios = await asyncio.to_thread(
            load_scenarios,
            root,
            categories=[category] if category else None,
            limit=bounded,
        )
    except ScenarioInvalid as exc:
        # "We could not read the catalogue" is not "the catalogue is empty".
        log.warning("scenario catalogue unavailable", path=str(root), error=exc.message)
        return {
            "available": False,
            "reason": exc.message,
            "items": [],
            "count": 0,
        }

    items = [
        {
            "id": s.id,
            "title": s.title,
            "category": s.category.value,
            "workload": s.workload.value,
            "difficulty": s.difficulty.value,
            "severity": s.severity.value,
            "environment": s.environment,
            "version": s.version,
            "tags": list(s.tags),
            # The digest a result row references, so the UI can tell an operator
            # that a historical number was produced against a different revision
            # of the same scenario.
            "content_hash": s.content_hash,
        }
        for s in scenarios
    ]
    return {
        "available": True,
        "reason": "",
        "items": items,
        "count": len(items),
        "truncated": len(items) >= bounded,
    }


# --------------------------------------------------------------------------- #
# comparison                                                                   #
# --------------------------------------------------------------------------- #


async def _digest(db: Database, run_id: str) -> ReportDigest:
    """Build the comparable surface of a persisted run.

    Deliberately the same ``ReportDigest`` the offline comparator consumes, so
    the release-gate arithmetic - including what counts as a safety regression -
    has exactly one implementation.
    """
    run = await _require_run(db, run_id)
    rows = await _results_for(db, run_id, limit=MAX_RESULT_ROWS)
    aggregates = _metric_aggregates(rows)
    summary = run.get("summary") or {}

    return ReportDigest(
        run_id=str(run["id"]),
        suite=str(run["suite"]),
        ablation=str(run["ablation"]),
        pass_rate=summary.get("pass_rate"),
        metrics={name: agg["mean"] for name, agg in aggregates.items()},
        sample_sizes={name: int(agg["n"]) for name, agg in aggregates.items()},
        determinism={name: str(agg["determinism"]) for name, agg in aggregates.items()},
        unsafe_scenarios=tuple(
            str(r["scenario_id"])
            for r in rows
            if r["unsafe"] and not r["harness_failure"]
        ),
        scenario_hashes={
            str(r["scenario_id"]): str(r["scenario_hash"] or "") for r in rows
        },
    )


@router.get("/compare", summary="Per-metric deltas between two runs")
async def compare_runs(
    _: RequireViewer,
    db: DbDep,
    base: Annotated[str, Query(min_length=1, max_length=64)],
    candidate: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict[str, Any]:
    """Candidate against base, with ``safety_regressed`` decided independently.

    An aggregate improvement never licenses a safety regression (PRD 9.3), so
    the flag is computed from the safety metrics and the newly-unsafe scenario
    list alone - it cannot be offset by a better pass rate.
    """
    base_digest = await _digest(db, base)
    candidate_digest = await _digest(db, candidate)
    comparison = compare(
        candidate_digest, base_digest, label=f"{candidate} vs {base}"
    )
    return comparison.as_json()
