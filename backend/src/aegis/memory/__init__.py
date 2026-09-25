"""Structured incident memory.

Write through ``IncidentMemoryStore`` (which refuses unverified knowledge), read
through ``IncidentMemoryRecall``. The contamination gate lives on the write path
precisely so that no reader has to decide whether a memory is trustworthy.
"""

from __future__ import annotations

from aegis.memory.recall import (
    CONFIDENCE_SIGNATURE,
    CONFIDENCE_SIMILARITY_MAX,
    MATCH_SIGNATURE,
    MATCH_SIMILARITY,
    IncidentMemoryRecall,
    MemoryMatch,
    RecallResult,
    RecurringPattern,
)
from aegis.memory.store import (
    SIGNATURE_VERSION,
    IncidentMemory,
    IncidentMemoryStore,
    MemoryContaminationError,
    normalise_symptom,
    recurrence_signature,
)

__all__ = [
    "CONFIDENCE_SIGNATURE",
    "CONFIDENCE_SIMILARITY_MAX",
    "MATCH_SIGNATURE",
    "MATCH_SIMILARITY",
    "SIGNATURE_VERSION",
    "IncidentMemory",
    "IncidentMemoryRecall",
    "IncidentMemoryStore",
    "MemoryContaminationError",
    "MemoryMatch",
    "RecallResult",
    "RecurringPattern",
    "normalise_symptom",
    "recurrence_signature",
]
