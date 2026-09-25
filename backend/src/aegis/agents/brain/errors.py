"""Typed failures of a brain tier.

A tier raises ``BrainUnavailable``; the router catches it and moves down a
tier. Nothing above the router ever sees it, because the Brain protocol
promises a decision on every step.
"""

from __future__ import annotations

from aegis.core.errors import ExternalServiceError


class BrainUnavailable(ExternalServiceError):
    """One tier could not produce a decision for this step.

    Not retryable: the next tier *is* the retry. Re-asking the same provider
    inside one step would spend the step's deadline on the tier already known
    to be failing.
    """

    code = "BRAIN_UNAVAILABLE"
    retryable = False


__all__ = ["BrainUnavailable"]
