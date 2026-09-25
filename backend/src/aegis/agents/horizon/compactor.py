"""Raw tool output -> one ``EvidenceCard`` of at most ~60 tokens.

Structured outputs (metric summaries, instance lists, deployments, query rows)
go through rule templates: exact, instant, no model. Unstructured text (logs,
web pages) goes to the compactor LLM when one is configured and answering, and
otherwise to a keyword/pattern extractor that cannot fail.

Two things are never taken from the model even on the LLM path:

* the card's ``weight`` - it feeds derived confidence, so it is assigned by
  code from where the observation came from;
* hypothesis ids it did not receive - an invented id is dropped, not trusted.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Final

from aegis.agents.horizon.context import estimate_tokens
from aegis.agents.horizon.phases import GAP_PREFIX
from aegis.agents.horizon.ports import CompactionInput, CompactorLLM
from aegis.core.logging import get_logger
from aegis.domain.horizon import MAX_CARD_CLAIM_CHARS, EvidenceCard, Source

log = get_logger(__name__)

MAX_CARD_TOKENS: Final = 60
_CLAIM_CHARS: Final = min(MAX_CARD_CLAIM_CHARS, MAX_CARD_TOKENS * 4)

# Signals worth surfacing from free text, in priority order. Deliberately a
# closed list: extraction must be predictable enough to test.
_SIGNALS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("leak", re.compile(r"\bleak\w*", re.I)),
    (
        "pool exhausted",
        re.compile(r"pool\W+(?:\w+\W+){0,3}exhaust\w*|exhaust\w*\W+(?:\w+\W+){0,3}pool", re.I),
    ),
    ("pool", re.compile(r"\b(?:connection\s+)?pool\b", re.I)),
    ("timeout", re.compile(r"time[d\s-]*out", re.I)),
    ("OOM", re.compile(r"\bOOM\b|out of memory", re.I)),
    ("refused", re.compile(r"connection refused", re.I)),
    ("exception", re.compile(r"\bexception\b|traceback", re.I)),
    ("error", re.compile(r"\berror\b", re.I)),
)
_VERSION: Final = re.compile(r"\b\d+\.\d+\.\d+\b")
_CVE: Final = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)


def _fit(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _CLAIM_CHARS else text[: _CLAIM_CHARS - 1] + "~"


def _num(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "n/a"
    return f"{value:.3g}"


class Compactor:
    """Implements the compaction policy in §9 of the design."""

    __slots__ = ("_llm",)

    def __init__(self, llm: CompactorLLM | None = None) -> None:
        self._llm = llm

    async def compact(
        self,
        item: CompactionInput,
        *,
        weight: float,
        unstructured: bool,
        gap_reason: str | None = None,
    ) -> EvidenceCard:
        """Always returns a card. A gap is a card too, marked ``UNAVAILABLE``."""
        tokens_raw = estimate_tokens(item.raw)
        supports: list[str] = []
        refutes: list[str] = []
        source = Source.RULE

        if gap_reason is not None:
            # "Could not look" is never compacted into "found nothing".
            claim = _fit(f"{GAP_PREFIX}: {item.tool} not consulted - {gap_reason}")
            weight = 0.0
        elif unstructured:
            llm_out = await self._llm_compact(item)
            if llm_out is not None:
                claim, supports, refutes = llm_out
                source = Source.GEMINI
            else:
                claim = self.rule_unstructured(item)
        else:
            claim = self.rule_structured(item)

        return EvidenceCard(
            id=item.evidence_id,
            step=item.step,
            tool=item.tool,
            source=source,
            origin=item.origin,
            claim=claim,
            supports=supports[:4],
            refutes=refutes[:4],
            weight=max(0.0, min(1.0, weight)),
            raw_ref=item.evidence_id,
            url=item.url,
            tokens_raw=tokens_raw,
            tokens_card=estimate_tokens(claim),
        )

    async def _llm_compact(self, item: CompactionInput) -> tuple[str, list[str], list[str]] | None:
        if self._llm is None or not self._llm.configured:
            return None
        try:
            out = await self._llm.compact(item)
        except Exception as exc:  # noqa: BLE001 - the port promises None, not raises
            log.warning("compactor llm failed; using rules", error=type(exc).__name__)
            return None
        if not out or not isinstance(out.get("claim"), str) or not out["claim"].strip():
            return None
        allowed = set(item.hypothesis_ids)
        supports = [s for s in out.get("supports") or [] if isinstance(s, str) and s in allowed]
        refutes = [s for s in out.get("refutes") or [] if isinstance(s, str) and s in allowed]
        return _fit(out["claim"]), supports, refutes

    # ---- rule paths -------------------------------------------------------- #

    @staticmethod
    def rule_unstructured(item: CompactionInput) -> str:
        """Keyword and pattern extraction. Exact counts, no interpretation."""
        text = item.raw
        lines = [ln for ln in text.splitlines() if ln.strip()]
        counts: Counter[str] = Counter()
        for label, pattern in _SIGNALS:
            n = len(pattern.findall(text))
            if n:
                counts[label] = n
        versions = sorted(set(_VERSION.findall(text)))[:3]
        cves = sorted({c.upper() for c in _CVE.findall(text)})[:2]
        parts = [f"{len(lines)} lines"]
        if counts:
            parts.append("signals: " + ", ".join(f"{k} x{v}" for k, v in counts.most_common(4)))
        else:
            parts.append("no failure keywords")
        if versions:
            parts.append("versions " + "/".join(versions))
        if cves:
            parts.append(" ".join(cves))
        head = f"{item.tool}"
        if item.structured and item.structured.get("title"):
            head += f" '{str(item.structured['title'])[:60]}'"
        return _fit(f"{head}: " + "; ".join(parts))

    @staticmethod
    def rule_structured(item: CompactionInput) -> str:
        s = item.structured or {}
        kind = s.get("kind")
        if kind == "metric":
            return _fit(
                f"{s.get('service')} {s.get('metric')} latest={_num(s.get('latest'))} "
                f"peak={_num(s.get('peak'))} mean={_num(s.get('mean'))} "
                f"({s.get('point_count', 0)} pts/{s.get('window_s', 0)}s)"
                if s.get("point_count")
                else f"{s.get('service')} {s.get('metric')}: no data in window (source answered)"
            )
        if kind == "instances":
            insts = s.get("instances") or []
            if not insts:
                return _fit(f"{s.get('service')}: no instances found (runtime answered)")
            desc = ", ".join(
                f"{i.get('name')} {i.get('health')} v{i.get('version') or '?'} "
                f"restarts={i.get('restart_count', 0)}"
                for i in insts[:3]
            )
            return _fit(f"{s.get('service')} instances: {desc}")
        if kind == "deployments":
            dep = (
                f" dep={s['dependency']}@{s.get('dependency_version') or '?'}"
                if s.get("dependency")
                else ""
            )
            prior = "/".join(str(v) for v in (s.get("history") or [])[:4]) or "none recorded"
            return _fit(
                f"{s.get('service')} running {s.get('current') or 'unknown'}"
                f" (image {s.get('image') or '?'}); history {prior}{dep}"
            )
        if kind == "query":
            rows = s.get("rows") or []
            if not rows:
                return _fit(f"{s.get('name')}: 0 rows (query answered)")
            first = rows[0] if isinstance(rows[0], dict) else {}
            cells = ", ".join(f"{k}={v}" for k, v in list(first.items())[:5])
            return _fit(f"{s.get('name')}: {len(rows)} rows; top {cells}")
        if kind == "verification":
            return _fit(str(s.get("summary", "")))
        if kind == "traces":
            return _fit(
                f"{s.get('service')} traces: {s.get('count', 0)} found, "
                f"slowest {_num(s.get('slowest_ms'))}ms"
            )
        # Generic: top-level scalars as key=value, deterministic order.
        cells = ", ".join(
            f"{k}={v}"
            for k, v in sorted(s.items())
            if isinstance(v, str | int | float | bool) and k != "kind"
        )
        return _fit(f"{item.tool}: {cells or 'no structured fields'}")


__all__ = ["MAX_CARD_TOKENS", "Compactor"]
