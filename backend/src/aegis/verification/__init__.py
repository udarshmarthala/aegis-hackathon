"""Deterministic verification of remediation outcomes."""

from aegis.verification.claims import (
    ClaimResult,
    ClaimTest,
    VerificationClaim,
    decide_verdict,
    summarise,
)
from aegis.verification.engine import (
    Baseline,
    VerificationEngine,
    VerificationRun,
    claims_from_plan,
)
from aegis.verification.store import VerificationStore

__all__ = [
    "Baseline",
    "ClaimResult",
    "ClaimTest",
    "VerificationClaim",
    "VerificationEngine",
    "VerificationRun",
    "VerificationStore",
    "claims_from_plan",
    "decide_verdict",
    "summarise",
]
