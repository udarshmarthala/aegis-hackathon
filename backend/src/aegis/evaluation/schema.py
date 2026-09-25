"""The ground-truth scenario format.

Two things live in this module and they are deliberately different types:

``Scenario``
    The full benchmark case: what fault an *independent* injector applies, what
    alert fires, and what the correct answer is. It exists only inside the
    harness process.

``ScenarioInput``
    The redacted view handed to the system under test. It carries an alert and
    nothing else - no scenario id, no category, no fault spec, no ground truth.

**The invariant.** Aegis must never receive a hidden label telling it which
fault was injected (ESD section 16). A benchmark that leaks the answer measures
nothing, and the leak is always accidental: a scenario id like ``CACHE-001``, a
``category`` field copied into an alert label, a fault ``mode`` echoed in an
annotation. So the separation is structural rather than a convention:

* ``ScenarioInput`` is a closed pydantic model with ``extra="forbid"``. There is
  no field on it that could carry ground truth, so nothing can be attached.
* ``Scenario.to_input()`` is the only constructor the harness uses, and it runs
  ``assert_sealed`` before returning - every string reachable from the input is
  checked against the scenario's sealed tokens (its category, root-cause
  category, fault mode and remediation category).
* The scenario id itself is replaced by ``case_ref``, an opaque digest, because
  ids encode their category by convention.

Service names are intentionally *not* sealed tokens: a real alert names the
service it fired on, which is often - legitimately - the faulty one. What must
never leak is the *diagnosis*.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticError

from aegis.core.errors import ValidationError
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType, EvidenceType, Severity, SourceType

log = get_logger(__name__)

# A benchmark directory is operator-controlled, but "bound everything" applies
# to it too: a runaway glob must not load an unbounded number of files.
MAX_SCENARIO_FILES: Final = 2000
SCHEMA_VERSION: Final = "1.0.0"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Workload(StrEnum):
    """Which reference application the scenario runs against."""

    SOCIALNETWORK = "socialnetwork"
    HOTELRESERVATION = "hotelreservation"
    REFERENCE = "reference"  # the gateway/checkout/payment workload in infra/docker


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class ScenarioCategory(StrEnum):
    """Closed set. A new category is a deliberate change to the benchmark."""

    LATENCY = "latency_increase"
    APPLICATION_ERRORS = "application_errors"
    DEPENDENCY_FAILURE = "dependency_failure"
    POD_FAILURE = "pod_failure"
    CPU_SATURATION = "cpu_saturation"
    MEMORY_PRESSURE = "memory_pressure"
    DATABASE_SATURATION = "database_saturation"
    CONNECTION_POOL = "connection_pool_exhaustion"
    CACHE_FAILURE = "cache_failure"
    NETWORKING = "networking"
    BAD_DEPLOYMENT = "bad_deployment"
    BAD_CONFIGURATION = "bad_configuration"
    CASCADING_FAILURE = "cascading_failure"
    AMBIGUOUS = "ambiguous_symptoms"
    CORRELATED_FAILURES = "multiple_correlated_failures"
    INDEPENDENT_FAULTS = "multiple_independent_faults"
    FALSE_POSITIVE = "false_positive_alert"
    SELF_RECOVERY = "recovery_without_intervention"
    REMEDIATION_REGRESSION = "remediation_regression"


class FaultMode(StrEnum):
    """What the injector does to the workload. Never visible to Aegis."""

    NONE = "none"  # false-positive scenarios inject nothing at all
    LATENCY = "latency"
    ERROR = "error"
    POOL_EXHAUSTION = "pool_exhaustion"
    PROCESS_KILL = "process_kill"
    CPU_BURN = "cpu_burn"
    MEMORY_LEAK = "memory_leak"
    DISK_PRESSURE = "disk_pressure"
    PACKET_LOSS = "packet_loss"
    DNS_FAILURE = "dns_failure"
    DEPENDENCY_OUTAGE = "dependency_outage"
    CACHE_FLUSH = "cache_flush"
    DB_SATURATION = "db_saturation"
    CONFIG_CHANGE = "config_change"
    BAD_DEPLOY = "bad_deploy"
    CLOCK_SKEW = "clock_skew"
    THREAD_STARVATION = "thread_starvation"


ParamValue = str | int | float | bool


class SecondaryFault(Frozen):
    """An additional, simultaneous fault (correlated or independent)."""

    target: str
    mode: FaultMode
    magnitude_ms: int = Field(default=0, ge=0, le=600_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    start_offset_s: int = Field(default=0, ge=0, le=3600)
    # True when this fault shares a cause with the primary one. The distinction
    # is the whole point of the "correlated" vs "independent" categories.
    correlated_with_primary: bool = True


class FaultInjection(Frozen):
    """What the independent injector applies, and how.

    ``control`` names the mechanism, not Aegis: the reference workload exposes
    ``/admin/fault``; other modes are applied by the compose/runtime layer. The
    harness owns this object; the system under test never sees it.
    """

    target: str
    mode: FaultMode
    magnitude_ms: int = Field(default=0, ge=0, le=600_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_s: int = Field(default=0, ge=0, le=7200)
    start_offset_s: int = Field(default=0, ge=0, le=3600)
    # Secondary faults for multi-fault scenarios. Bounded: a scenario with a
    # dozen simultaneous faults is not a benchmark case, it is a chaos run.
    secondary: tuple[SecondaryFault, ...] = ()
    control: str = Field(default="workload_admin_api", max_length=64)
    parameters: dict[str, ParamValue] = Field(default_factory=dict, max_length=16)

    @field_validator("secondary")
    @classmethod
    def _bounded_secondary(cls, v: tuple[SecondaryFault, ...]) -> tuple[SecondaryFault, ...]:
        if len(v) > 4:
            raise ValueError("at most 4 secondary faults per scenario")
        return v

    @model_validator(mode="after")
    def _magnitude_matches_mode(self) -> FaultInjection:
        if self.mode is FaultMode.LATENCY and self.magnitude_ms <= 0:
            raise ValueError("a latency fault needs magnitude_ms > 0")
        if self.mode is FaultMode.ERROR and self.error_rate <= 0.0:
            raise ValueError("an error fault needs error_rate > 0")
        if self.mode is FaultMode.NONE and (self.magnitude_ms or self.error_rate):
            raise ValueError("fault mode 'none' must not carry a magnitude")
        return self


class AlertSpec(Frozen):
    """The only thing Aegis is given.

    Annotations are operator free text and reach Aegis as Tier-D
    ``UntrustedText``; nothing here may hint at the injected fault.
    """

    title: str = Field(min_length=4, max_length=200)
    severity: Severity
    source: str = Field(default="prometheus", max_length=64)
    service_hint: str | None = None
    metric: str | None = None
    labels: dict[str, str] = Field(default_factory=dict, max_length=24)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=12)
    # Wall-clock delay between fault injection and the alert firing, so the
    # harness reproduces the detection lag a real alert rule introduces.
    fires_after_s: int = Field(default=60, ge=0, le=1800)


class ExpectedEvidence(Frozen):
    """One observation a competent investigation should have collected."""

    source_type: SourceType
    evidence_type: EvidenceType
    resource_id: str | None = None
    required: bool = True

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.source_type.value, self.evidence_type.value, self.resource_id)


class GroundTruth(Frozen):
    """The answer key. Never reachable from ``ScenarioInput``."""

    affected_services: tuple[str, ...] = ()
    root_cause_service: str | None = None
    root_cause_category: str | None = Field(default=None, max_length=64)
    root_cause_statement: str = Field(default="", max_length=2000)
    # Ordered chain from the true origin to the symptom the alert fired on.
    causal_dependency: tuple[str, ...] = ()
    expected_evidence: tuple[ExpectedEvidence, ...] = ()
    expected_blast_radius: tuple[str, ...] = ()
    forbidden_actions: tuple[ActionType, ...] = ()
    expected_safe_actions: tuple[ActionType, ...] = ()
    expected_remediation_category: str | None = Field(default=None, max_length=64)
    expected_verification_criteria: tuple[str, ...] = ()
    # Honest-uncertainty outcomes. A benchmark that cannot detect a system which
    # never abstains cannot tell a careful diagnostician from a fluent guesser.
    should_abstain: bool = False
    is_false_positive: bool = False
    recovers_without_intervention: bool = False
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _outcome_consistency(self) -> GroundTruth:
        if self.is_false_positive:
            if self.expected_safe_actions:
                raise ValueError("a false-positive alert has no correct remediation action")
            if self.expected_remediation_category is not None:
                raise ValueError("a false-positive alert has no remediation category")
        if self.should_abstain and self.expected_safe_actions:
            raise ValueError(
                "a scenario that should abstain cannot also expect a remediation action"
            )
        if not self.should_abstain and not self.is_false_positive:
            if self.root_cause_service is None:
                raise ValueError("a conclusive scenario needs root_cause_service")
            if not self.root_cause_category:
                raise ValueError("a conclusive scenario needs root_cause_category")
        overlap = set(self.forbidden_actions) & set(self.expected_safe_actions)
        if overlap:
            raise ValueError(
                "an action cannot be both forbidden and expected: "
                + ", ".join(sorted(a.value for a in overlap))
            )
        if (
            self.causal_dependency
            and self.root_cause_service is not None
            and self.causal_dependency[0] != self.root_cause_service
        ):
            raise ValueError(
                "causal_dependency must start at root_cause_service "
                f"({self.root_cause_service!r}), got {self.causal_dependency[0]!r}"
            )
        return self

    @property
    def required_evidence(self) -> tuple[ExpectedEvidence, ...]:
        return tuple(e for e in self.expected_evidence if e.required)


class ScenarioInput(Frozen):
    """Everything the system under test is allowed to know.

    Closed by construction (``extra="forbid"``). Adding a field here is the only
    way to widen what Aegis learns about a benchmark case, which makes widening
    it a reviewable act rather than an accident.
    """

    case_ref: str = Field(min_length=8, max_length=32)
    alert_title: str
    severity: Severity
    source: str
    environment: str
    workload: str
    service_hint: str | None = None
    metric: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    # Operator free text. The caller wraps these in ``UntrustedText`` before any
    # of it reaches a prompt (CLAUDE.md invariant 7).
    annotations: dict[str, str] = Field(default_factory=dict)

    def texts(self) -> Iterator[str]:
        """Every string a prompt could ever see. Used by the leak check."""
        yield self.alert_title
        yield self.source
        yield self.environment
        yield self.workload
        if self.service_hint:
            yield self.service_hint
        if self.metric:
            yield self.metric
        for key, value in self.labels.items():
            yield key
            yield value
        for key, value in self.annotations.items():
            yield key
            yield value


class GroundTruthLeak(ValidationError):
    """Raised when a scenario's answer key is reachable from its input."""

    code = "GROUND_TRUTH_LEAK"


class ScenarioInvalid(ValidationError):
    """A scenario file failed schema validation. Names the file and the field."""

    code = "SCENARIO_INVALID"


class Scenario(Frozen):
    """One benchmark case: fault, alert and answer key."""

    id: str = Field(pattern=r"^[A-Z]{2,6}-[A-Z0-9]{2,12}-\d{3}$")
    title: str = Field(min_length=4, max_length=200)
    category: ScenarioCategory
    description: str = Field(min_length=10, max_length=4000)
    workload: Workload
    severity: Severity
    difficulty: Difficulty = Difficulty.MEDIUM
    environment: str = Field(default="local", max_length=32)
    version: int = Field(default=1, ge=1)
    tags: tuple[str, ...] = ()
    fault: FaultInjection
    alert: AlertSpec
    ground_truth: GroundTruth
    # Provenance, filled by the loader. Excluded from the content hash so moving
    # a file does not invalidate historical results.
    source_file: str = ""

    @field_validator("tags")
    @classmethod
    def _bounded_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > 12:
            raise ValueError("at most 12 tags")
        return v

    @model_validator(mode="after")
    def _category_matches_outcome(self) -> Scenario:
        gt = self.ground_truth
        if self.category is ScenarioCategory.FALSE_POSITIVE and not gt.is_false_positive:
            raise ValueError("a false_positive_alert scenario must set is_false_positive")
        if gt.is_false_positive and self.fault.mode is not FaultMode.NONE:
            raise ValueError("a false-positive scenario must not inject a fault")
        if (
            self.category is ScenarioCategory.SELF_RECOVERY
            and not gt.recovers_without_intervention
        ):
            raise ValueError(
                "a recovery_without_intervention scenario must set recovers_without_intervention"
            )
        return self

    # ---- derived ------------------------------------------------------------

    @property
    def content_hash(self) -> str:
        """Stable digest of the scored content.

        Results reference this so a report can prove which revision of a
        scenario produced a number. Editing a scenario changes the hash, and a
        comparison across that boundary is flagged rather than silently averaged.
        """
        payload = self.model_dump(mode="json", exclude={"source_file"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    @property
    def case_ref(self) -> str:
        """Opaque handle for the scenario.

        Deliberately not the id: ids carry their category in the prefix, and an
        agent that can read ``CACHE-REDIS-004`` has been told the answer.
        """
        return hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:16]

    def sealed_tokens(self) -> frozenset[str]:
        """Lowercased strings that must never appear in the system's input.

        Categories, fault modes, causal chains and remediation categories are
        the diagnosis. Service names are excluded on purpose - an alert names
        the service it fired on, and that is a real signal, not a leak.
        """
        tokens: set[str] = {self.category.value, self.fault.mode.value}
        gt = self.ground_truth
        for value in (gt.root_cause_category, gt.expected_remediation_category):
            if value:
                tokens.add(value)
        for fault in self.fault.secondary:
            tokens.add(fault.mode.value)
        if gt.root_cause_statement:
            tokens.add(gt.root_cause_statement)
        # Verification criteria are metric names. A real alert fires *on* a
        # metric, so the metric name is a legitimate part of the input and
        # sealing it would make every honest scenario look like a leak.
        # Single short words like "none" or "error" collide with ordinary alert
        # text; only distinctive multi-part tokens are checkable.
        return frozenset(
            t.lower() for t in tokens if len(t) >= 8 and ("_" in t or " " in t or "-" in t)
        )

    def to_input(self) -> ScenarioInput:
        """Build the redacted view, then prove it carries no answer."""
        payload = ScenarioInput(
            case_ref=self.case_ref,
            alert_title=self.alert.title,
            severity=self.alert.severity,
            source=self.alert.source,
            environment=self.environment,
            workload=self.workload.value,
            service_hint=self.alert.service_hint,
            metric=self.alert.metric,
            labels=dict(self.alert.labels),
            annotations=dict(self.alert.annotations),
        )
        assert_sealed(self, payload)
        return payload


def assert_sealed(scenario: Scenario, payload: ScenarioInput) -> None:
    """Fail loudly when the answer key is reachable from the alert.

    Called on every ``to_input()`` rather than only in tests: a leak discovered
    after a benchmark run has already invalidated the run.
    """
    tokens = scenario.sealed_tokens()
    if not tokens:
        return
    found: list[str] = []
    for text in payload.texts():
        lowered = text.lower()
        found.extend(token for token in tokens if token in lowered)
    if found:
        raise GroundTruthLeak(
            f"scenario {scenario.id}: ground truth leaked into the system input",
            context={"scenario_id": scenario.id, "tokens": sorted(set(found))},
        )


# --------------------------------------------------------------------------- #
# loading                                                                      #
# --------------------------------------------------------------------------- #


def _format_pydantic_error(path: Path, exc: PydanticError) -> str:
    lines: list[str] = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  field '{loc}': {err['msg']}")
    return f"{path}: invalid scenario\n" + "\n".join(lines)


def parse_scenario(data: Any, *, source: Path) -> Scenario:
    """Validate one already-parsed document into a ``Scenario``."""
    if not isinstance(data, dict):
        raise ScenarioInvalid(
            f"{source}: expected a YAML mapping at the top level, got {type(data).__name__}",
            context={"file": str(source)},
        )
    try:
        scenario = Scenario.model_validate({**data, "source_file": source.name})
    except PydanticError as exc:
        raise ScenarioInvalid(
            _format_pydantic_error(source, exc),
            context={"file": str(source), "errors": exc.error_count()},
        ) from exc
    # A scenario whose input leaks the answer is broken at authoring time, not
    # at run time; surfacing it here keeps a bad file out of the corpus.
    scenario.to_input()
    return scenario


def load_scenario_file(path: Path) -> Scenario:
    """Load and validate a single scenario file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioInvalid(
            f"{path}: cannot be read ({exc.strerror or exc})", context={"file": str(path)}
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ScenarioInvalid(
            f"{path}: not valid YAML - {exc}", context={"file": str(path)}
        ) from exc
    if data is None:
        raise ScenarioInvalid(f"{path}: file is empty", context={"file": str(path)})
    return parse_scenario(data, source=path)


def load_scenarios(
    root: Path,
    *,
    categories: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[Scenario]:
    """Load every scenario under ``root``, sorted by id.

    Duplicate ids are rejected: two files claiming the same case would make a
    result row ambiguous, and results are the product here.
    """
    if not root.is_dir():
        raise ScenarioInvalid(
            f"{root}: scenario directory does not exist", context={"path": str(root)}
        )
    paths = sorted(p for p in root.rglob("*.yaml") if p.is_file())
    if len(paths) > MAX_SCENARIO_FILES:
        raise ScenarioInvalid(
            f"{root}: {len(paths)} scenario files exceeds the {MAX_SCENARIO_FILES} cap",
            context={"path": str(root), "count": len(paths)},
        )

    wanted = {c.strip().lower() for c in categories} if categories else None
    seen: dict[str, str] = {}
    scenarios: list[Scenario] = []
    for path in paths:
        scenario = load_scenario_file(path)
        if scenario.id in seen:
            raise ScenarioInvalid(
                f"{path}: duplicate scenario id {scenario.id!r} "
                f"(already defined in {seen[scenario.id]})",
                context={"file": str(path), "scenario_id": scenario.id},
            )
        seen[scenario.id] = path.name
        if wanted is not None and scenario.category.value not in wanted:
            continue
        scenarios.append(scenario)

    scenarios.sort(key=lambda s: s.id)
    if limit is not None and limit > 0:
        scenarios = scenarios[:limit]
    log.info(
        "scenarios loaded",
        root=str(root),
        files=len(paths),
        selected=len(scenarios),
        categories=sorted(wanted) if wanted else "all",
    )
    return scenarios


__all__ = [
    "MAX_SCENARIO_FILES",
    "SCHEMA_VERSION",
    "AlertSpec",
    "Difficulty",
    "ExpectedEvidence",
    "FaultInjection",
    "FaultMode",
    "GroundTruth",
    "GroundTruthLeak",
    "Scenario",
    "ScenarioCategory",
    "ScenarioInput",
    "ScenarioInvalid",
    "SecondaryFault",
    "Workload",
    "assert_sealed",
    "load_scenario_file",
    "load_scenarios",
    "parse_scenario",
]
