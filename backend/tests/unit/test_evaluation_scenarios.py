"""The scenario corpus and the rule that keeps it honest.

Every file in ``eval/scenarios`` is validated here, individually, so a broken
scenario fails in a two-second unit test instead of forty minutes into a
benchmark run. The rest of the module tests the boundary the whole benchmark
rests on: ground truth cannot reach the system under test.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from aegis.domain.enums import ActionType, EvidenceType, Severity, SourceType
from aegis.evaluation.schema import (
    AlertSpec,
    ExpectedEvidence,
    FaultInjection,
    FaultMode,
    GroundTruth,
    GroundTruthLeak,
    Scenario,
    ScenarioCategory,
    ScenarioInput,
    ScenarioInvalid,
    Workload,
    load_scenario_file,
    load_scenarios,
    parse_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_ROOT = REPO_ROOT / "eval" / "scenarios"
SCENARIO_FILES = sorted(SCENARIO_ROOT.rglob("*.yaml"))


def _scenario(**overrides) -> Scenario:
    base = {
        "id": "TST-REF-001",
        "title": "A synthetic scenario for tests",
        "category": ScenarioCategory.LATENCY,
        "description": "A latency fault used by the schema tests.",
        "workload": Workload.REFERENCE,
        "severity": Severity.P2,
        "fault": FaultInjection(target="payment", mode=FaultMode.LATENCY, magnitude_ms=400),
        "alert": AlertSpec(title="Gateway p99 latency above budget", severity=Severity.P2,
                        service_hint="gateway"),
        "ground_truth": GroundTruth(
            affected_services=("gateway", "payment"),
            root_cause_service="payment",
            root_cause_category="upstream_latency",
            causal_dependency=("payment", "gateway"),
            expected_evidence=(
                ExpectedEvidence(source_type=SourceType.METRICS,
                                 evidence_type=EvidenceType.METRIC_SERIES),
            ),
        ),
    }
    base.update(overrides)
    return Scenario(**base)


# --------------------------------------------------------------------------- #
# the real corpus                                                              #
# --------------------------------------------------------------------------- #


def test_the_corpus_exists() -> None:
    assert SCENARIO_FILES, f"no scenarios found under {SCENARIO_ROOT}"


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.stem)
def test_every_scenario_file_validates(path: Path) -> None:
    """Parametrised over the real files: one failure names one scenario."""
    scenario = load_scenario_file(path)
    assert scenario.id == path.stem
    # Directory layout is part of the format: a scenario filed under the wrong
    # category quietly skews every per-category aggregate.
    assert scenario.category.value == path.parent.name
    assert scenario.source_file == path.name


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.stem)
def test_no_scenario_leaks_its_answer_into_the_alert(path: Path) -> None:
    scenario = load_scenario_file(path)
    payload = scenario.to_input()  # raises GroundTruthLeak if it does
    blob = " ".join(payload.texts()).lower()
    for token in scenario.sealed_tokens():
        assert token not in blob, f"{scenario.id} leaks {token!r}"
    # Nothing on the redacted view can carry the answer key, by construction.
    keys = set(payload.model_dump().keys())
    assert not keys & {"ground_truth", "fault", "category", "id", "notes"}


def test_corpus_is_broad_enough_to_mean_something() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    categories = Counter(s.category.value for s in scenarios)
    assert len(scenarios) >= 40
    assert len(categories) >= 15
    # A benchmark made only of solvable incidents rewards confident guessing.
    assert sum(1 for s in scenarios if s.ground_truth.should_abstain) >= 2
    assert sum(1 for s in scenarios if s.ground_truth.is_false_positive) >= 2
    assert sum(
        1 for s in scenarios if s.ground_truth.recovers_without_intervention
    ) >= 2
    # And it must contain cases that are hard, not just numerous.
    assert sum(1 for s in scenarios if s.difficulty.value == "hard") >= 8


def test_corpus_ids_are_unique_and_hashes_are_distinct() -> None:
    scenarios = load_scenarios(SCENARIO_ROOT)
    ids = [s.id for s in scenarios]
    assert len(set(ids)) == len(ids)
    hashes = [s.content_hash for s in scenarios]
    assert len(set(hashes)) == len(hashes), "two scenarios have identical content"
    refs = [s.case_ref for s in scenarios]
    assert len(set(refs)) == len(refs)


def test_false_positive_scenarios_inject_nothing_and_expect_no_action() -> None:
    for scenario in load_scenarios(SCENARIO_ROOT):
        if not scenario.ground_truth.is_false_positive:
            continue
        assert scenario.fault.mode is FaultMode.NONE
        assert scenario.ground_truth.expected_safe_actions == ()
        assert scenario.ground_truth.root_cause_service is None


def test_forbidden_actions_are_real_action_types() -> None:
    known = {a.value for a in ActionType}
    for scenario in load_scenarios(SCENARIO_ROOT):
        for action in (*scenario.ground_truth.forbidden_actions,
                       *scenario.ground_truth.expected_safe_actions):
            assert action.value in known


def test_loading_one_category_filters_without_reordering() -> None:
    cached = load_scenarios(SCENARIO_ROOT, categories=["cache_failure"])
    assert cached
    assert {s.category.value for s in cached} == {"cache_failure"}
    assert [s.id for s in cached] == sorted(s.id for s in cached)
    assert len(load_scenarios(SCENARIO_ROOT, limit=3)) == 3


# --------------------------------------------------------------------------- #
# ground truth cannot reach the system under test                              #
# --------------------------------------------------------------------------- #


def test_scenario_input_is_closed_against_extra_fields() -> None:
    with pytest.raises(Exception, match="extra"):
        ScenarioInput(
            case_ref="abcdef0123456789",
            alert_title="something",
            severity=Severity.P2,
            source="prometheus",
            environment="local",
            workload="reference",
            root_cause_service="payment",  # the leak this test exists to stop
        )


def test_a_leaked_root_cause_category_fails_to_load() -> None:
    with pytest.raises(GroundTruthLeak) as excinfo:
        _scenario(
            alert=AlertSpec(
                title="Gateway degraded",
                severity=Severity.P2,
                annotations={"hint": "this is an upstream_latency problem"},
            )
        ).to_input()
    assert "upstream_latency" in str(excinfo.value.context["tokens"])


def test_a_leaked_fault_mode_fails_to_load() -> None:
    with pytest.raises(GroundTruthLeak):
        _scenario(
            category=ScenarioCategory.CONNECTION_POOL,
            fault=FaultInjection(target="checkout", mode=FaultMode.POOL_EXHAUSTION),
            alert=AlertSpec(title="checkout pool_exhaustion detected",
                            severity=Severity.P2),
            ground_truth=GroundTruth(
                affected_services=("checkout",),
                root_cause_service="checkout",
                root_cause_category="pool_exhaustion",
            ),
        ).to_input()


def test_case_ref_hides_the_scenario_id() -> None:
    scenario = _scenario()
    payload = scenario.to_input()
    assert payload.case_ref != scenario.id
    assert scenario.id not in payload.case_ref
    # Stable across loads so results can be correlated.
    assert payload.case_ref == _scenario().to_input().case_ref


def test_service_names_are_not_treated_as_a_leak() -> None:
    """An alert naming the faulty service is a real signal, not a leak."""
    payload = _scenario(
        alert=AlertSpec(title="payment latency above budget", severity=Severity.P2,
                        service_hint="payment")
    ).to_input()
    assert payload.service_hint == "payment"


# --------------------------------------------------------------------------- #
# loader errors                                                                #
# --------------------------------------------------------------------------- #


def test_malformed_yaml_names_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "BAD-REF-001.yaml"
    bad.write_text("id: BAD-REF-001\n  bad indentation: [unclosed\n", encoding="utf-8")
    with pytest.raises(ScenarioInvalid) as excinfo:
        load_scenario_file(bad)
    message = str(excinfo.value)
    assert "BAD-REF-001.yaml" in message
    assert "not valid YAML" in message


def test_missing_field_names_the_field(tmp_path: Path) -> None:
    bad = tmp_path / "BAD-REF-002.yaml"
    bad.write_text(
        "id: BAD-REF-002\n"
        "title: Missing most of the required structure\n"
        "category: latency_increase\n"
        "workload: reference\n"
        "severity: P2\n"
        "description: A scenario with no fault, alert or ground truth.\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioInvalid) as excinfo:
        load_scenario_file(bad)
    message = str(excinfo.value)
    assert "BAD-REF-002.yaml" in message
    assert "field 'fault'" in message
    assert "field 'alert'" in message


def test_unknown_enum_value_is_rejected_with_the_field_name(tmp_path: Path) -> None:
    bad = tmp_path / "BAD-REF-003.yaml"
    bad.write_text(
        "id: BAD-REF-003\n"
        "title: Uses a category that does not exist\n"
        "category: someone_invented_this\n"
        "workload: reference\n"
        "severity: P2\n"
        "description: The category vocabulary is closed on purpose.\n"
        "fault: {target: payment, mode: latency, magnitude_ms: 100}\n"
        "alert: {title: Gateway slow, severity: P2}\n"
        "ground_truth: {root_cause_service: payment, root_cause_category: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioInvalid) as excinfo:
        load_scenario_file(bad)
    assert "field 'category'" in str(excinfo.value)


def test_empty_file_and_missing_directory_are_distinguished(tmp_path: Path) -> None:
    empty = tmp_path / "EMPTY-REF-001.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ScenarioInvalid, match="file is empty"):
        load_scenario_file(empty)
    with pytest.raises(ScenarioInvalid, match="does not exist"):
        load_scenarios(tmp_path / "nope")


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    body = (SCENARIO_FILES[0]).read_text(encoding="utf-8")
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ScenarioInvalid, match="duplicate scenario id"):
        load_scenarios(tmp_path)


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ScenarioInvalid, match="mapping"):
        parse_scenario(["not", "a", "mapping"], source=tmp_path / "x.yaml")


# --------------------------------------------------------------------------- #
# ground-truth self-consistency                                                #
# --------------------------------------------------------------------------- #


def test_a_false_positive_cannot_expect_a_remediation() -> None:
    with pytest.raises(Exception, match="no correct remediation action"):
        GroundTruth(is_false_positive=True,
                    expected_safe_actions=(ActionType.RESTART_INSTANCE,))


def test_an_abstaining_scenario_cannot_expect_an_action() -> None:
    with pytest.raises(Exception, match="cannot also expect a remediation"):
        GroundTruth(should_abstain=True,
                    expected_safe_actions=(ActionType.RERUN_HEALTH_CHECK,))


def test_a_conclusive_scenario_needs_a_root_cause() -> None:
    with pytest.raises(Exception, match="needs root_cause_service"):
        GroundTruth(affected_services=("gateway",))


def test_an_action_cannot_be_both_forbidden_and_expected() -> None:
    with pytest.raises(Exception, match="cannot be both forbidden and expected"):
        GroundTruth(root_cause_service="payment", root_cause_category="x",
                    forbidden_actions=(ActionType.RESTART_INSTANCE,),
                    expected_safe_actions=(ActionType.RESTART_INSTANCE,))


def test_causal_chain_must_start_at_the_root_cause() -> None:
    with pytest.raises(Exception, match="must start at root_cause_service"):
        GroundTruth(root_cause_service="payment", root_cause_category="x",
                    causal_dependency=("gateway", "payment"))


def test_a_false_positive_scenario_may_not_inject_a_fault() -> None:
    with pytest.raises(Exception, match="must not inject a fault"):
        _scenario(
            category=ScenarioCategory.FALSE_POSITIVE,
            ground_truth=GroundTruth(is_false_positive=True),
        )


def test_latency_fault_requires_a_magnitude() -> None:
    with pytest.raises(Exception, match="needs magnitude_ms"):
        FaultInjection(target="payment", mode=FaultMode.LATENCY)


def test_content_hash_tracks_scored_content_but_not_file_location() -> None:
    a = _scenario()
    b = _scenario(source_file="moved.yaml")
    assert a.content_hash == b.content_hash
    c = _scenario(title="A different question entirely")
    assert c.content_hash != a.content_hash
