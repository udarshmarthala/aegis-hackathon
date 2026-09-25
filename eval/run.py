#!/usr/bin/env python
"""Benchmark entry point.

    python eval/run.py --suite smoke
    python eval/run.py --suite full --ablation no_graph --out eval/reports/no_graph.json
    python eval/run.py --suite cache_failure --limit 2 --dry-run
    python eval/run.py --suite full --resume evr_01J... --compare eval/reports/last.json
    python eval/run.py --suite full --allow-uninjectable   # injectable subset only

Every run prints a preflight first: how many of the selected scenarios this
environment can actually inject a fault for, and which ones it cannot. A
scenario whose fault mode has no injector here is recorded as a harness failure
and excluded from the quality aggregates - or, with --allow-uninjectable,
dropped from the suite and listed as not measured. It is never scored as though
the fault had been applied.

Runs against the local stack and needs no cloud credentials: Postgres is the
only hard dependency, and even that is optional with ``--no-persist`` (the
report still prints, it is just not recorded).

Exit codes are part of the contract, because CI reads them:

    0  the run completed and no scenario was unsafe
    1  the run could not start (bad arguments, unreadable scenarios, no stack)
    2  at least one scenario was unsafe - a forbidden, unapproved or tier-3
       action executed, or a failed action was not rolled back
    3  a safety metric regressed against the baseline passed to --compare

2 and 3 are deliberately separate from "quality got worse". Quality is a
judgement call for a human reading the report; an unsafe action is not.

Argument parsing happens before any Aegis import so ``--help`` works in a bare
interpreter, which matters when someone is trying to find out how to run this
and the virtualenv is not active yet.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# ruff: noqa: T201 - this is a CLI; printing the report is the point.

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
if str(REPO_ROOT / "eval") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "eval"))

DEFAULT_SCENARIOS = REPO_ROOT / "eval" / "scenarios"
DEFAULT_REPORT_DIR = REPO_ROOT / "eval" / "reports"
# A smoke suite has to be small enough to run on every push and broad enough to
# be worth running: one scenario from each category, easiest first.
SMOKE_PER_CATEGORY = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSAFE = 2
EXIT_SAFETY_REGRESSION = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval/run.py",
        description="Run the Aegis ground-truth incident benchmark.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "suites:\n"
            "  smoke        one scenario per category (fast, runs in CI)\n"
            "  full         every scenario in eval/scenarios\n"
            "  <category>   one category, e.g. cache_failure or bad_deployment\n"
        ),
    )
    parser.add_argument("--suite", default="smoke",
                        help="smoke | full | <category> (default: smoke)")
    parser.add_argument("--ablation", default="full",
                        help="architecture configuration to run (default: full)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of scenarios")
    parser.add_argument("--resume", dest="resume_run_id", default=None,
                        help="continue an interrupted run by its evaluation run id")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the JSON report here (Markdown alongside it)")
    parser.add_argument("--compare", type=Path, default=None,
                        help="a previous JSON report to compare against")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS,
                        help=f"scenario directory (default: {DEFAULT_SCENARIOS})")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="scenarios in flight at once (default: 2)")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="per-scenario timeout in seconds (default: 900)")
    parser.add_argument("--no-persist", action="store_true",
                        help="do not write results to Postgres")
    parser.add_argument("--no-inject", action="store_true",
                        help="skip fault injection (the workload is already faulted)")
    parser.add_argument("--allow-uninjectable", action="store_true",
                        help=("drop scenarios whose fault mode this injector cannot "
                              "apply instead of recording them as harness failures; "
                              "every dropped scenario id is printed and written to "
                              "the report"))
    parser.add_argument("--dry-run", action="store_true",
                        help="load and validate scenarios, then exit without running")
    parser.add_argument("--list-ablations", action="store_true",
                        help="print the available ablation names and exit")
    return parser


def select(scenarios: list, suite: str, limit: int | None) -> list:
    """Pick the scenarios a suite runs. Deterministic, so runs are comparable."""
    if suite == "full":
        chosen = list(scenarios)
    elif suite == "smoke":
        order = {"easy": 0, "medium": 1, "hard": 2}
        by_category: dict[str, list] = {}
        for scenario in scenarios:
            by_category.setdefault(scenario.category.value, []).append(scenario)
        chosen = []
        for category in sorted(by_category):
            ranked = sorted(
                by_category[category],
                key=lambda s: (order.get(s.difficulty.value, 3), s.id),
            )
            chosen.extend(ranked[:SMOKE_PER_CATEGORY])
    else:
        chosen = [s for s in scenarios if s.category.value == suite]
    chosen.sort(key=lambda s: s.id)
    if limit and limit > 0:
        chosen = chosen[:limit]
    return chosen


def partition_injectable(
    scenarios: list, injector_type: Any, injector: Any = None
) -> tuple[list, list]:
    """Split a selection into what this injector can apply and what it cannot.

    Two independent reasons a scenario cannot run, and both have to be checked:

    * **the mode** - read from ``WorkloadFaultInjector.supported_modes()``
      rather than a copy kept here, so a workload that learns a new fault mode
      is reflected in the preflight the same day it is implemented;
    * **the target** - whether a host exists for the service the fault names.

    Checking only the mode is how a preflight comes to report twenty-one
    runnable scenarios on a machine where thirteen can run: every hotelReservation
    and socialNetwork scenario passed the mode check while naming a service that
    resolved to nothing. The run then spent its timeout budget failing to
    connect, which is a slow, expensive way to learn something a preflight can
    say in a second.

    Returns ``(injectable, blocked)`` where ``blocked`` carries the reason
    alongside the scenario, because "which ones, and why" is what an operator
    needs before waiting out a run - a bare count is not actionable.
    """
    injectable: list = []
    blocked: list[tuple[object, tuple]] = []
    for scenario in scenarios:
        missing = injector_type.unsupported_modes(scenario)
        if missing:
            blocked.append((scenario, missing))
            continue
        if injector is not None:
            targets = [scenario.fault.target, *(f.target for f in scenario.fault.secondary)]
            unreachable = tuple(
                t for t in dict.fromkeys(targets)
                if t and t != "none" and not injector.knows(t, scenario.workload)
            )
            if unreachable:
                blocked.append((scenario, unreachable))
                continue
        injectable.append(scenario)
    return injectable, blocked


def _print_preflight(selected: list, blocked: list, *, allow_uninjectable: bool) -> None:
    """Say what this run can actually inject, before anyone waits for it."""
    print(f"preflight: {len(selected)} scenario(s) selected, "
          f"{len(selected) - len(blocked)} injectable here, {len(blocked)} not")
    if not blocked:
        return
    def _label(item: Any) -> str:
        return item.value if hasattr(item, "value") else f"no host for {item!r}"

    reasons = sorted({_label(m) for _, missing in blocked for m in missing})
    print(f"  blocked by: {', '.join(reasons)}")
    for scenario, missing in blocked:
        print(f"    {scenario.id:16} {scenario.workload:18} "
              f"{', '.join(_label(m) for m in missing)}")
    if allow_uninjectable:
        print(f"  --allow-uninjectable: dropping {len(blocked)} scenario(s); "
              "this run measures the injectable subset only")
    else:
        print(f"  {len(blocked)} scenario(s) will be attempted and recorded as harness "
              "failures (ENVIRONMENT_FAILURE), excluded from quality aggregates;\n"
              "  pass --allow-uninjectable to drop them from the suite instead")


async def _run(args: argparse.Namespace) -> int:
    from aegis.core.config import get_settings
    from aegis.core.errors import AegisError
    from aegis.core.logging import configure_logging
    from aegis.evaluation.ablations import ablation_names, get_ablation
    from aegis.evaluation.harness import BenchmarkHarness, HarnessConfig
    from aegis.evaluation.report import (
        compare,
        load_json,
        to_markdown,
        write_json,
        write_markdown,
    )
    from aegis.evaluation.schema import ScenarioCategory, load_scenarios

    if args.list_ablations:
        for name in ablation_names():
            print(f"{name:26} {get_ablation(name).description}")
        return EXIT_OK

    valid_suites = {"smoke", "full", *(c.value for c in ScenarioCategory)}
    if args.suite not in valid_suites:
        print(f"unknown suite {args.suite!r}; expected one of: "
              f"{', '.join(sorted(valid_suites))}", file=sys.stderr)
        return EXIT_ERROR

    try:
        get_ablation(args.ablation)
        scenarios = load_scenarios(args.scenarios)
    except AegisError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR

    selected = select(scenarios, args.suite, args.limit)
    if not selected:
        print(f"no scenarios matched suite {args.suite!r}", file=sys.stderr)
        return EXIT_ERROR

    from injector import WorkloadFaultInjector

    # Preflight before anything is started. Waiting fifteen minutes to discover
    # that half the suite injected nothing is the failure mode this prevents.
    dropped: tuple[str, ...] = ()
    if args.no_inject:
        print("preflight skipped: --no-inject asserts the workload is already faulted")
    else:
        injectable, blocked = partition_injectable(
            selected, WorkloadFaultInjector, WorkloadFaultInjector()
        )
        _print_preflight(selected, blocked, allow_uninjectable=args.allow_uninjectable)
        if blocked and args.allow_uninjectable:
            dropped = tuple(s.id for s, _ in blocked)
            selected = injectable
        if not selected:
            print("no injectable scenario left in this suite", file=sys.stderr)
            return EXIT_ERROR

    if args.dry_run:
        print(f"{len(selected)} scenario(s) validated for suite {args.suite!r}:")
        for scenario in selected:
            flags = []
            if scenario.ground_truth.should_abstain:
                flags.append("abstain")
            if scenario.ground_truth.is_false_positive:
                flags.append("false-positive")
            if scenario.ground_truth.recovers_without_intervention:
                flags.append("self-recovering")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {scenario.id:16} {scenario.difficulty.value:6} "
                  f"{scenario.category.value:30} {scenario.title}{suffix}")
        return EXIT_OK

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return await _execute(args, settings, selected, dropped,
                          helpers=(BenchmarkHarness, HarnessConfig, compare, load_json,
                                   to_markdown, write_json, write_markdown))


async def _execute(args, settings, selected, dropped, helpers) -> int:  # noqa: ANN001
    # Classes and functions, so the capitals are correct; N806 reads a tuple
    # unpack as a constant assignment and cannot tell the difference.
    (BenchmarkHarness, HarnessConfig, compare, load_json, to_markdown,  # noqa: N806
     write_json, write_markdown) = helpers

    from aegis.core.errors import AegisError
    from aegis.evaluation.harness import NullEnvironment
    from aegis.evidence.store import EvidenceStore
    from aegis.evidence.validator import EvidenceValidator
    from aegis.integrations.langsmith import LangSmithIntegration
    from aegis.persistence.db import Database
    from aegis_sut import AegisSystemUnderTest
    from injector import WorkloadFaultInjector

    db = Database(settings)
    persist = not args.no_persist
    try:
        await db.connect()
    except (AegisError, OSError) as exc:
        # Postgres is not optional for a benchmark run, and calling this
        # "running without persistence" was wrong in a way that cost a whole
        # suite: the adapter reads results back OUT of Postgres - evidence
        # items, tool calls, agent runs - precisely so it scores what the system
        # recorded rather than what the agent said about itself. With no
        # database every scenario becomes an ENVIRONMENT_FAILURE and the run
        # measures nothing, so it must not start.
        print(
            f"fatal: cannot reach Postgres at {settings.postgres_host}:"
            f"{settings.postgres_port} ({exc})",
            file=sys.stderr,
        )
        print(
            "  The harness reads scenario results back out of the database, so a "
            "run without it scores nothing.",
            file=sys.stderr,
        )
        print(
            "  Running from the host against the compose stack? .env points at "
            "the compose hostname; override with:",
            file=sys.stderr,
        )
        print(
            "    POSTGRES_HOST=localhost POSTGRES_PORT=55433 python eval/run.py ...",
            file=sys.stderr,
        )
        return 2

    sut = AegisSystemUnderTest(settings, db)
    # --no-inject means the workload is already in the faulted state the
    # scenario describes, so the injector is left out of the run entirely
    # rather than being asked to inject nothing.
    #
    # strict is on unless the operator asked for the injectable subset, in which
    # case the uninjectable scenarios have already been dropped above and strict
    # has nothing left to refuse. It stays on either way for a mode that slips
    # through: silently injecting nothing is never a run this tool will produce.
    injector = WorkloadFaultInjector()
    environment = NullEnvironment() if args.no_inject else injector
    langsmith = LangSmithIntegration(settings)
    if langsmith.configured:
        langsmith.enable()

    config = HarnessConfig(
        suite=args.suite,
        ablation=args.ablation,
        concurrency=max(1, args.concurrency),
        scenario_timeout_s=max(1.0, args.timeout),
        persist=persist,
        resume_run_id=args.resume_run_id,
        agent_version=settings.aegis_version,
        model=settings.llm_model_reasoning,
        uninjectable_scenarios=dropped,
    )
    harness = BenchmarkHarness(
        config,
        sut,
        db=db if persist else None,
        environment=environment,
        validator=EvidenceValidator(EvidenceStore(db)) if db.is_ready else None,
        langsmith=langsmith,
    )

    try:
        report = await harness.run(selected)
    except asyncio.CancelledError:
        print(f"cancelled; resume with --resume {harness.run_id}", file=sys.stderr)
        raise
    finally:
        await injector.close()
        await sut.close()
        if db.is_ready:
            await db.close()

    comparison = None
    if args.compare is not None:
        try:
            comparison = compare(report, load_json(args.compare), label="vs baseline")
        except AegisError as exc:
            print(f"warning: comparison skipped ({exc.message})", file=sys.stderr)

    out = args.out or (DEFAULT_REPORT_DIR / f"{args.suite}-{args.ablation}.json")
    write_json(report, out)
    write_markdown(report, out.with_suffix(".md"), comparison=comparison)
    print(to_markdown(report, comparison=comparison))
    if dropped:
        # Loud, last, and next to the numbers it qualifies: every metric above
        # was computed without these scenarios, and a reader who misses that
        # will read the suite name as coverage it does not have.
        print(f"\n## Not measured\n\n**{len(dropped)} scenario(s) were dropped "
              "before the run: this injector cannot apply their fault mode.** "
              "Nothing above includes them.\n")
        for scenario_id in dropped:
            print(f"- `{scenario_id}`")
    print(f"report: {out}\nrun id: {report.run_id}", file=sys.stderr)

    if report.unsafe_scenarios:
        print(f"FAIL: {len(report.unsafe_scenarios)} unsafe scenario(s)", file=sys.stderr)
        return EXIT_UNSAFE
    if comparison is not None and comparison.safety_regressed:
        print("FAIL: safety metrics regressed against the baseline", file=sys.stderr)
        return EXIT_SAFETY_REGRESSION
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The Markdown report contains non-ASCII separators; a Windows console in a
    # legacy code page must not be able to kill a finished benchmark run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    # Stop LangChain picking up ambient tracing configuration the operator did
    # not ask for: a benchmark must not ship prompts to a third party by accident.
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
