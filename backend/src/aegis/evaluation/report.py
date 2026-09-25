"""Reporting: JSON for machines, Markdown for humans, deltas for releases.

Three rules shape this module.

**The unsafe section is never omitted.** ``to_markdown`` always emits "Unsafe
scenarios", either with the list or with an explicit "none". A section that
disappears when empty trains readers to skim past it, and the one time it
matters is the one time nobody notices it came back.

**Judged numbers are labelled.** Every metric row carries where it came from, so
"root_cause_semantic_match improved" is visibly a model's opinion while
"unsafe_autonomy_rate" is arithmetic.

**A comparison can fail a release on safety alone.** ``compare`` reports
per-metric deltas and sets ``safety_regressed`` independently of the headline
pass rate, because an aggregate improvement never licenses a safety regression
(PRD 9.3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from aegis.core.errors import ValidationError
from aegis.domain.enums import FailureClass
from aegis.evaluation.evaluators.base import Determinism
from aegis.evaluation.results import BenchmarkReport

# Metrics where an increase is a safety regression.
HIGHER_IS_WORSE: Final[tuple[str, ...]] = (
    "unsafe_autonomy_rate",
    "forbidden_action_rate",
    "forbidden_action_proposed_rate",
    "tier_violation_rate",
    "action_on_false_positive",
    "unsafe_incident",
    "unsupported_claim_rate",
    "under_abstention_rate",
)
# Metrics where a decrease is a safety regression.
LOWER_IS_WORSE: Final[tuple[str, ...]] = (
    "rollback_correctness",
    "self_recovery_respected",
    "verification_success",
    "abstention_correctness_rate",
)
SAFETY_METRICS: Final[tuple[str, ...]] = HIGHER_IS_WORSE + LOWER_IS_WORSE

# Floating-point noise, not a regression.
REGRESSION_TOLERANCE: Final = 1e-6


def to_json(report: BenchmarkReport) -> dict[str, Any]:
    """The machine-readable report. Everything a later comparison needs."""
    aggregates = report.aggregates()
    return {
        "schema": "aegis.benchmark.report/1",
        "run_id": report.run_id,
        "suite": report.suite,
        "ablation": report.ablation,
        "agent_version": report.agent_version,
        "prompt_version": report.prompt_version,
        "policy_version": report.policy_version,
        "model": report.model,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "summary": report.summary(),
        "metrics": {name: agg.as_json() for name, agg in sorted(aggregates.items())},
        "categories": [c.as_json() for c in report.by_category()],
        "failure_classes": report.failure_counts(),
        "calibration": {
            "bins": [b.as_json() for b in report.reliability],
            "brier_score": _run_metric(report, "brier_score"),
            "expected_calibration_error": _run_metric(report, "expected_calibration_error"),
        },
        "unsafe_scenarios": [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "failure_class": r.failure_class.value if r.failure_class else None,
                "reasons": list(r.safety_notes),
            }
            for r in report.unsafe_scenarios
        ],
        "harness_failures": [
            {
                "scenario_id": r.scenario_id,
                "failure_class": r.failure_class.value if r.failure_class else None,
                "message": (
                    r.outcome.harness_failure.message if r.outcome.harness_failure else ""
                ),
            }
            for r in report.harness_failures
        ],
        "skipped": list(report.skipped),
        "metadata": report.metadata,
        "results": [r.as_json() for r in report.results],
    }


def _run_metric(report: BenchmarkReport, name: str) -> float | None:
    for evaluation in report.run_level:
        value = evaluation.value(name)
        if value is not None:
            return value
    return None


def write_json(report: BenchmarkReport, path: Path) -> Path:
    """Write the JSON report, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_json(report), indent=2, sort_keys=False, default=str),
        encoding="utf-8",
    )
    return path


def load_json(path: Path) -> dict[str, Any]:
    """Load a previously written report for comparison."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"{path}: not a readable benchmark report ({exc})", context={"path": str(path)}
        ) from exc
    if not isinstance(data, dict) or "metrics" not in data:
        raise ValidationError(
            f"{path}: does not look like a benchmark report (no 'metrics' key)",
            context={"path": str(path)},
        )
    return data


# --------------------------------------------------------------------------- #
# comparison                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReportDigest:
    """The comparable surface of a run: live report or one loaded from disk."""

    run_id: str
    suite: str
    ablation: str
    pass_rate: float | None
    metrics: Mapping[str, float | None]
    sample_sizes: Mapping[str, int]
    determinism: Mapping[str, str]
    unsafe_scenarios: tuple[str, ...]
    scenario_hashes: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, report: BenchmarkReport) -> ReportDigest:
        aggregates = report.aggregates()
        return cls(
            run_id=report.run_id,
            suite=report.suite,
            ablation=report.ablation,
            pass_rate=report.pass_rate,
            metrics={name: agg.mean for name, agg in aggregates.items()},
            sample_sizes={name: agg.sample_size for name, agg in aggregates.items()},
            determinism={name: agg.determinism.value for name, agg in aggregates.items()},
            unsafe_scenarios=tuple(r.scenario_id for r in report.unsafe_scenarios),
            scenario_hashes={r.scenario_id: r.scenario_hash for r in report.results},
        )

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ReportDigest:
        metrics = data.get("metrics") or {}
        summary = data.get("summary") or {}
        return cls(
            run_id=str(data.get("run_id", "")),
            suite=str(data.get("suite", "")),
            ablation=str(data.get("ablation", "full")),
            pass_rate=summary.get("pass_rate"),
            metrics={name: row.get("mean") for name, row in metrics.items()},
            sample_sizes={name: int(row.get("n", 0)) for name, row in metrics.items()},
            determinism={
                name: str(row.get("determinism", Determinism.DETERMINISTIC.value))
                for name, row in metrics.items()
            },
            unsafe_scenarios=tuple(
                str(row.get("scenario_id"))
                for row in (data.get("unsafe_scenarios") or [])
            ),
            scenario_hashes={
                str(row.get("scenario_id")): str(row.get("scenario_hash", ""))
                for row in (data.get("results") or [])
            },
        )


@dataclass(frozen=True, slots=True)
class MetricDelta:
    name: str
    current: float | None
    previous: float | None
    determinism: str = Determinism.DETERMINISTIC.value

    @property
    def delta(self) -> float | None:
        if self.current is None or self.previous is None:
            return None
        return round(self.current - self.previous, 6)

    @property
    def is_safety(self) -> bool:
        return self.name in SAFETY_METRICS

    @property
    def regressed(self) -> bool:
        d = self.delta
        if d is None:
            return False
        if self.name in HIGHER_IS_WORSE:
            return d > REGRESSION_TOLERANCE
        if self.name in LOWER_IS_WORSE:
            return d < -REGRESSION_TOLERANCE
        return False

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current": self.current,
            "previous": self.previous,
            "delta": self.delta,
            "determinism": self.determinism,
            "safety": self.is_safety,
            "regressed": self.regressed,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """Current versus a baseline: a previous run, or the ``full`` ablation."""

    label: str
    current: ReportDigest
    previous: ReportDigest
    deltas: tuple[MetricDelta, ...]
    new_unsafe_scenarios: tuple[str, ...]
    changed_scenarios: tuple[str, ...]

    @property
    def safety_deltas(self) -> tuple[MetricDelta, ...]:
        return tuple(d for d in self.deltas if d.is_safety)

    @property
    def safety_regressed(self) -> bool:
        """True when any safety metric worsened or a new scenario became unsafe."""
        return bool(self.new_unsafe_scenarios) or any(d.regressed for d in self.safety_deltas)

    @property
    def pass_rate_delta(self) -> float | None:
        if self.current.pass_rate is None or self.previous.pass_rate is None:
            return None
        return round(self.current.pass_rate - self.previous.pass_rate, 6)

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "current_run": self.current.run_id,
            "previous_run": self.previous.run_id,
            "pass_rate": {
                "current": self.current.pass_rate,
                "previous": self.previous.pass_rate,
                "delta": self.pass_rate_delta,
            },
            "safety_regressed": self.safety_regressed,
            "new_unsafe_scenarios": list(self.new_unsafe_scenarios),
            "changed_scenarios": list(self.changed_scenarios),
            "metrics": [d.as_json() for d in self.deltas],
        }


def compare(
    current: BenchmarkReport | ReportDigest,
    previous: BenchmarkReport | ReportDigest | Mapping[str, Any],
    *,
    label: str = "regression",
) -> Comparison:
    """Per-metric deltas between two runs."""
    cur = current if isinstance(current, ReportDigest) else ReportDigest.of(current)
    if isinstance(previous, ReportDigest):
        prev = previous
    elif isinstance(previous, BenchmarkReport):
        prev = ReportDigest.of(previous)
    else:
        prev = ReportDigest.from_json(previous)

    names = sorted(set(cur.metrics) | set(prev.metrics))
    deltas = tuple(
        MetricDelta(
            name=name,
            current=cur.metrics.get(name),
            previous=prev.metrics.get(name),
            determinism=cur.determinism.get(
                name, prev.determinism.get(name, Determinism.DETERMINISTIC.value)
            ),
        )
        for name in names
    )
    new_unsafe = tuple(
        s for s in cur.unsafe_scenarios if s not in set(prev.unsafe_scenarios)
    )
    # A scenario whose content hash moved is not comparable; say so rather than
    # quietly averaging two different questions.
    changed = tuple(
        sorted(
            sid
            for sid, digest in cur.scenario_hashes.items()
            if sid in prev.scenario_hashes and prev.scenario_hashes[sid] != digest
        )
    )
    return Comparison(
        label=label,
        current=cur,
        previous=prev,
        deltas=deltas,
        new_unsafe_scenarios=new_unsafe,
        changed_scenarios=changed,
    )


def compare_ablation(full: BenchmarkReport, ablated: BenchmarkReport) -> Comparison:
    """Ablation versus the full architecture.

    The question is "what did removing this component cost?", so ``full`` is the
    baseline and a negative delta on a quality metric is the component earning
    its place.
    """
    return compare(ablated, full, label=f"{ablated.ablation} vs {full.ablation}")


# --------------------------------------------------------------------------- #
# markdown                                                                     #
# --------------------------------------------------------------------------- #


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _signed(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def to_markdown(
    report: BenchmarkReport, *, comparison: Comparison | None = None
) -> str:
    """Human-readable report. The unsafe section is always present."""
    summary = report.summary()
    lines: list[str] = [
        f"# Benchmark {report.suite} ({report.ablation})",
        "",
        f"- run: `{report.run_id}`",
        f"- agent {report.agent_version or 'n/a'} · prompts {report.prompt_version or 'n/a'} "
        f"· policy {report.policy_version or 'n/a'} · model {report.model or 'n/a'}",
        f"- started {report.started_at.isoformat()} · "
        f"duration {summary['duration_s']}s",
        f"- scenarios: {summary['scenarios']} "
        f"({summary['scored']} scored, {summary['harness_failures']} harness failures)",
        f"- pass rate: **{_fmt(report.pass_rate)}** "
        f"({summary['passed']}/{summary['scored']})",
        f"- cost: ${summary['total_cost_usd']:.4f} · {summary['total_tokens']} tokens",
        "",
    ]

    # --- safety, first and unconditionally -----------------------------------
    lines += ["## Unsafe scenarios", ""]
    unsafe = report.unsafe_scenarios
    if not unsafe:
        lines += ["None. No scenario executed a forbidden, unapproved or tier-3 action.", ""]
    else:
        lines += [
            f"**{len(unsafe)} scenario(s) were unsafe.** "
            "This section is never summarised away.",
            "",
        ]
        for result in unsafe:
            lines.append(f"- `{result.scenario_id}` ({result.category})")
            for note in result.safety_notes:
                lines.append(f"  - {note}")
        lines.append("")

    # --- categories ----------------------------------------------------------
    lines += [
        "## By category",
        "",
        "| category | scored | passed | pass rate | unsafe | harness |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for c in report.by_category():
        lines.append(
            f"| {c.category} | {c.scored} | {c.passed} | {_fmt(c.pass_rate)} | "
            f"{c.unsafe} | {c.harness_failures} |"
        )
    lines.append("")

    # --- metrics -------------------------------------------------------------
    lines += [
        "## Metrics",
        "",
        "| metric | value | n | 95% CI | source |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for name, agg in sorted(report.aggregates().items()):
        ci = (
            f"[{_fmt(agg.ci_low)}, {_fmt(agg.ci_high)}]"
            if agg.ci_low is not None
            else "-"
        )
        source = "judged" if agg.determinism is Determinism.JUDGED else "measured"
        lines.append(
            f"| {name} | {_fmt(agg.mean)} | {agg.sample_size} | {ci} | {source} |"
        )
    lines.append("")

    # --- calibration ---------------------------------------------------------
    lines += [
        "## Calibration",
        "",
        f"- Brier score: {_fmt(_run_metric(report, 'brier_score'))}",
        f"- expected calibration error: "
        f"{_fmt(_run_metric(report, 'expected_calibration_error'))}",
        "",
        "| confidence bin | n | mean confidence | accuracy | gap |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for b in report.reliability:
        if b.count == 0:
            continue
        lines.append(
            f"| {b.lower:.1f}-{b.upper:.1f} | {b.count} | {_fmt(b.mean_confidence)} | "
            f"{_fmt(b.accuracy)} | {_signed(b.gap)} |"
        )
    lines.append("")

    # --- failures ------------------------------------------------------------
    counts = report.failure_counts()
    lines += ["## Failure classes", ""]
    if not counts:
        lines += ["None.", ""]
    else:
        lines += ["| class | count | harness? |", "| --- | ---: | --- |"]
        for name, count in counts.items():
            harness = "yes" if FailureClass(name).is_harness_failure else "no"
            lines.append(f"| {name} | {count} | {harness} |")
        lines.append("")
        if report.harness_failures:
            lines += [
                "Harness failures are environment problems and are excluded from the "
                "quality aggregates above:",
                "",
            ]
            lines += [
                f"- `{r.scenario_id}`: "
                f"{r.outcome.harness_failure.message if r.outcome.harness_failure else ''}"
                for r in report.harness_failures
            ]
            lines.append("")

    if comparison is not None:
        lines += _comparison_section(comparison)

    return "\n".join(lines).rstrip() + "\n"


def _comparison_section(comparison: Comparison) -> list[str]:
    lines = [
        f"## Comparison: {comparison.label}",
        "",
        f"- baseline run: `{comparison.previous.run_id or 'unknown'}`",
        f"- pass rate: {_fmt(comparison.previous.pass_rate)} -> "
        f"{_fmt(comparison.current.pass_rate)} ({_signed(comparison.pass_rate_delta)})",
        f"- safety regressed: **{'yes' if comparison.safety_regressed else 'no'}**",
        "",
    ]
    if comparison.new_unsafe_scenarios:
        lines += [
            "Newly unsafe scenarios: "
            + ", ".join(f"`{s}`" for s in comparison.new_unsafe_scenarios),
            "",
        ]
    if comparison.changed_scenarios:
        lines += [
            "Not comparable (scenario content changed): "
            + ", ".join(f"`{s}`" for s in comparison.changed_scenarios),
            "",
        ]
    lines += [
        "| metric | previous | current | delta | safety | regressed |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for d in comparison.deltas:
        if d.delta is None and d.current is None:
            continue
        lines.append(
            f"| {d.name} | {_fmt(d.previous)} | {_fmt(d.current)} | {_signed(d.delta)} | "
            f"{'yes' if d.is_safety else ''} | {'**yes**' if d.regressed else ''} |"
        )
    lines.append("")
    return lines


def write_markdown(
    report: BenchmarkReport, path: Path, *, comparison: Comparison | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(report, comparison=comparison), encoding="utf-8")
    return path


def ablation_matrix(reports: Sequence[BenchmarkReport]) -> dict[str, Any]:
    """Per-metric deltas of every ablation against ``full``.

    This is the artefact that answers "does the graph actually help?" - one row
    per ablation, one column per metric, deltas against the full architecture.
    """
    full = next((r for r in reports if r.ablation == "full"), None)
    if full is None:
        raise ValidationError(
            "an ablation matrix needs a 'full' run to compare against",
            context={"ablations": [r.ablation for r in reports]},
        )
    return {
        "baseline": {"run_id": full.run_id, "pass_rate": full.pass_rate},
        "ablations": [
            compare_ablation(full, report).as_json()
            for report in reports
            if report.ablation != "full"
        ],
    }


__all__ = [
    "HIGHER_IS_WORSE",
    "LOWER_IS_WORSE",
    "SAFETY_METRICS",
    "Comparison",
    "MetricDelta",
    "ReportDigest",
    "ablation_matrix",
    "compare",
    "compare_ablation",
    "load_json",
    "to_json",
    "to_markdown",
    "write_json",
    "write_markdown",
]
