"""The verification primitive: CLAIM + EVIDENCE + TEST + RESULT + TIMESTAMP.

A remediation is not verified because a command returned zero, and not because a
deployment went green. It is verified when the specific condition that defined
the incident is demonstrably gone, and nothing else broke in the process.

Expressing that as a list of independently testable claims has three payoffs an
aggregate boolean does not give:

* **Disputability.** An operator can disagree with one claim without rejecting
  the whole verdict, and can see exactly which measurement changed their mind.
* **Partial honesty.** When four claims pass and one could not be measured, the
  run reports ``PARTIALLY_VERIFIED`` with the gap named, rather than rounding up
  to success.
* **Regression detection.** Protected-metric claims are tested with the same
  machinery as the target metric, so "the error rate fell but latency doubled"
  is a first-class outcome instead of an unnoticed side effect.

Nothing in this module consults a model. Every claim resolves from a number, an
exit code or a probe result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aegis.domain.enums import (
    ClaimOutcome,
    MetricDirection,
    VerificationTestKind,
    VerificationVerdict,
)


@dataclass(frozen=True, slots=True)
class ClaimTest:
    """How a claim will be decided, stated before the measurement is taken.

    Declaring the test up front is what stops post-hoc rationalisation: the
    threshold cannot be chosen after seeing the result.
    """

    kind: VerificationTestKind
    metric: str | None = None
    resource_id: str | None = None
    direction: MetricDirection | None = None
    threshold: float | None = None
    tolerance: float = 0.0
    window_seconds: int = 300
    spec: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "metric": self.metric,
            "resource_id": self.resource_id,
            "direction": self.direction.value if self.direction else None,
            "threshold": self.threshold,
            "tolerance": self.tolerance,
            "window_seconds": self.window_seconds,
            **({"spec": self.spec} if self.spec else {}),
        }


@dataclass(frozen=True, slots=True)
class VerificationClaim:
    """One falsifiable statement about the state of the system after a change."""

    id: str
    statement: str
    test: ClaimTest
    protected: bool = False  # a "nothing else broke" claim rather than a goal

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("a verification claim needs a statement")


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """The measured answer to one claim, with the numbers that produced it."""

    claim: VerificationClaim
    outcome: ClaimOutcome
    before_value: float | None
    after_value: float | None
    threshold: float | None
    evidence_ids: list[str]
    detail: str
    observed_at: datetime

    @property
    def passed(self) -> bool:
        return self.outcome is ClaimOutcome.PASS

    @property
    def is_regression(self) -> bool:
        """A protected metric that failed is a regression, not a missed goal."""
        return self.claim.protected and self.outcome is ClaimOutcome.FAIL

    def as_json(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim.id,
            "claim": self.claim.statement,
            "protected": self.claim.protected,
            "test": self.claim.test.as_json(),
            "outcome": self.outcome.value,
            "before": self.before_value,
            "after": self.after_value,
            "threshold": self.threshold,
            "evidence_ids": list(self.evidence_ids),
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
        }


def decide_verdict(results: list[ClaimResult]) -> VerificationVerdict:
    """Combine claim results into one verdict. Pure, total and order-independent.

    Precedence, strongest signal first:

    1. Any protected metric failing is a REGRESSION_DETECTED, even if every goal
       claim passed. Fixing the symptom while breaking something else is not a
       successful remediation.
    2. Any goal claim failing is FAILED.
    3. No claims at all is INCONCLUSIVE. An empty claim set has never been
       evidence of health, and returning VERIFIED here would let a remediation
       with no verification plan appear proven.
    4. Every claim passing is VERIFIED.
    5. Anything else - some passes plus unavailable or inconclusive claims - is
       PARTIALLY_VERIFIED. Deliberately not a success: an unmeasured claim is an
       open question, not a quiet yes.
    """
    if not results:
        return VerificationVerdict.INCONCLUSIVE

    if any(r.is_regression for r in results):
        return VerificationVerdict.REGRESSION_DETECTED

    if any(r.outcome is ClaimOutcome.FAIL for r in results):
        return VerificationVerdict.FAILED

    if all(r.outcome is ClaimOutcome.PASS for r in results):
        return VerificationVerdict.VERIFIED

    goal_results = [r for r in results if not r.claim.protected]
    if goal_results and all(
        r.outcome in (ClaimOutcome.UNAVAILABLE, ClaimOutcome.INCONCLUSIVE)
        for r in goal_results
    ):
        # Nothing about the actual goal was measurable. Reporting a partial
        # verification here would overstate what is known.
        return VerificationVerdict.INCONCLUSIVE

    return VerificationVerdict.PARTIALLY_VERIFIED


def summarise(results: list[ClaimResult]) -> str:
    """One line an operator can read without opening the detail view."""
    if not results:
        return "no verification claims were evaluated"
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
    parts = [f"{n} {name.lower()}" for name, n in sorted(counts.items())]
    regressions = [r for r in results if r.is_regression]
    line = f"{len(results)} claims: " + ", ".join(parts)
    if regressions:
        names = ", ".join(r.claim.test.metric or r.claim.id for r in regressions)
        line += f"; regression on {names}"
    return line


__all__ = [
    "ClaimResult",
    "ClaimTest",
    "VerificationClaim",
    "decide_verdict",
    "summarise",
]
