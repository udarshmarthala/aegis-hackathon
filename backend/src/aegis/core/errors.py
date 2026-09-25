"""Typed error hierarchy.

Every failure in Aegis is one of these. Two fields drive platform behaviour:

``retryable``  - whether ``core.resilience.retry_async`` may retry it. Anything
                 that mutates non-idempotent external state is never retryable.
``code``       - a stable machine identifier surfaced to the API and the UI, so
                 operators see a real cause instead of "internal error".
"""

from __future__ import annotations

from typing import Any


class AegisError(Exception):
    """Base for every Aegis failure."""

    code: str = "AEGIS_ERROR"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.context: dict[str, Any] = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --- configuration ----------------------------------------------------------


class ConfigError(AegisError):
    """Invalid or missing configuration. Always fatal at boot - fail closed."""

    code = "CONFIG_INVALID"
    http_status = 500


# --- domain -----------------------------------------------------------------


class DomainError(AegisError):
    """A domain rule was violated (illegal transition, invalid aggregate)."""

    code = "DOMAIN_INVARIANT"
    http_status = 409


class NotFoundError(AegisError):
    code = "NOT_FOUND"
    http_status = 404


class ValidationError(AegisError):
    code = "VALIDATION_FAILED"
    http_status = 422


# --- authn / authz ----------------------------------------------------------


class AuthenticationError(AegisError):
    """Caller identity could not be established."""

    code = "UNAUTHENTICATED"
    http_status = 401


class AuthorizationError(AegisError):
    """Identity established, authority denied. Never conflated with the above."""

    code = "FORBIDDEN"
    http_status = 403


# --- safety -----------------------------------------------------------------


class PolicyViolation(AegisError):
    """The policy engine blocked an action. Never retried, never overridden."""

    code = "POLICY_BLOCKED"
    http_status = 403


class EvidenceError(AegisError):
    """A claim cited evidence that does not validate. Degrades to abstention."""

    code = "EVIDENCE_INVALID"
    http_status = 422


class LeaseConflict(AegisError):
    """Another actor holds the lease on this resource. Concurrency guard fired."""

    code = "LEASE_CONFLICT"
    http_status = 409


class BudgetExhausted(AegisError):
    """An agent budget ran out. Preserve state, conclude, escalate."""

    code = "BUDGET_EXHAUSTED"
    http_status = 200


# --- external boundaries ----------------------------------------------------


class ExternalServiceError(AegisError):
    """An integration failed in a way that may succeed on retry."""

    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 502
    retryable = True


class SourceUnavailable(ExternalServiceError):
    """An evidence source could not be queried.

    This is emphatically NOT "no evidence found". It becomes a recorded evidence
    gap that lowers confidence and is rendered distinctly in the UI (PRD 13).
    """

    code = "SOURCE_UNAVAILABLE"
    http_status = 503


class SourceNotConfigured(SourceUnavailable):
    """The evidence source is not deployed here, so nothing was attempted.

    Still a ``SourceUnavailable`` - and still coded ``SOURCE_UNAVAILABLE`` - so
    every caller keeps recording an evidence gap rather than reading the absence
    as "found nothing". What it adds is the reason: "not deployed" and "down"
    call for different operator responses, and a deployment that deliberately
    omits Neo4j should not look like one whose Neo4j has crashed.

    Never retryable. Retrying a missing setting cannot make it present, and
    each retry is only boot latency.
    """

    retryable = False

    @classmethod
    def for_setting(cls, dependency: str, setting: str) -> SourceNotConfigured:
        """The one wording every client uses, so the UI sees a single phrasing."""
        return cls(
            f"{dependency} not deployed ({setting} is empty)",
            context={"dependency": dependency, "setting": setting, "not_configured": True},
        )


def is_unset(value: str) -> bool:
    """Whether a connection setting means "this dependency is not deployed".

    Whitespace counts as empty: an env var rendered from a blank template
    variable is as absent as one that was never set, and treating it as a
    hostname would only produce a connection error with a misleading cause.
    """
    return not value.strip()


class CircuitOpen(ExternalServiceError):
    """The breaker is open; the call was rejected without attempting it."""

    code = "CIRCUIT_OPEN"
    http_status = 503
    retryable = False


class TimeoutExceeded(ExternalServiceError):
    code = "TIMEOUT"
    http_status = 504
