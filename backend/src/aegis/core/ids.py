"""Stable, sortable identifiers.

ULIDs are used rather than UUID4 because they are lexicographically sortable by
creation time. Timeline and audit queries then use the primary key index
directly instead of a secondary sort on ``created_at``.
"""

from __future__ import annotations

from typing import Final

from ulid import ULID

INCIDENT: Final = "inc"
EVIDENCE: Final = "ev"
HYPOTHESIS: Final = "hyp"
ACTION: Final = "act"
APPROVAL: Final = "apr"
AGENT_RUN: Final = "run"
TOOL_CALL: Final = "tc"
VERIFICATION: Final = "ver"
LEASE: Final = "lse"
MEMORY: Final = "mem"
SANDBOX: Final = "sbx"
EVAL_RUN: Final = "evr"
AUDIT: Final = "aud"

_PREFIXES: Final = frozenset(
    {
        INCIDENT, EVIDENCE, HYPOTHESIS, ACTION, APPROVAL, AGENT_RUN,
        TOOL_CALL, VERIFICATION, LEASE, MEMORY, SANDBOX, EVAL_RUN, AUDIT,
    }
)


def new_id(prefix: str) -> str:
    """Return a new prefixed ULID, e.g. ``inc_01J8Z3...``."""
    if prefix not in _PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}_{ULID()}"


def is_id(value: str, prefix: str) -> bool:
    """True when ``value`` is a well-formed id carrying ``prefix``."""
    head, _, tail = value.partition("_")
    if head != prefix or not tail:
        return False
    try:
        ULID.from_str(tail)
    except (ValueError, TypeError):
        return False
    return True


def correlation_id() -> str:
    """Opaque id used to stitch logs, traces, audit rows and LangSmith runs."""
    return str(ULID())
