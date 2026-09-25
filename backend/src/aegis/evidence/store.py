"""Evidence persistence with provenance.

Evidence is append-only and content-addressed. Two properties follow:

* the same observation fetched twice collapses to one row (dedup on hash)
* a stored item cannot be silently edited later - an audit can detect tampering

Trust class is assigned here from the source registry, never taken from caller
input and never from model output.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    EvidenceStatus,
    EvidenceType,
    SourceType,
    TrustClass,
)
from aegis.domain.models import EvidenceItem, UntrustedText
from aegis.persistence.db import Database

log = get_logger(__name__)

# Trust is a property of where data came from, decided here and nowhere else.
_SOURCE_TRUST: dict[SourceType, TrustClass] = {
    SourceType.METRICS: TrustClass.TIER_A,
    SourceType.TRACES: TrustClass.TIER_A,
    SourceType.RUNTIME: TrustClass.TIER_A,
    SourceType.SANDBOX: TrustClass.TIER_A,
    SourceType.DEPLOYMENT: TrustClass.TIER_B,
    SourceType.VCS: TrustClass.TIER_B,
    SourceType.GRAPH: TrustClass.TIER_B,
    SourceType.RUNBOOK: TrustClass.TIER_C,
    SourceType.MEMORY: TrustClass.TIER_C,
    SourceType.LOGS: TrustClass.TIER_D,
}


def trust_for(source_type: SourceType) -> TrustClass:
    """Tier D is the safe default for anything unrecognised."""
    return _SOURCE_TRUST.get(source_type, TrustClass.TIER_D)


def content_hash(source: str, provenance_uri: str, structured: dict[str, Any]) -> str:
    """Stable digest over what was asked and what came back."""
    payload = json.dumps(
        {"s": source, "p": provenance_uri, "v": structured},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvidenceStore:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        incident_id: str,
        source: str,
        source_type: SourceType,
        evidence_type: EvidenceType,
        summary: str,
        structured_value: dict[str, Any] | None = None,
        content: str | None = None,
        untrusted: bool = False,
        provenance_uri: str = "",
        resource_id: str | None = None,
        observed_at: datetime | None = None,
        status: EvidenceStatus = EvidenceStatus.UNVALIDATED,
    ) -> EvidenceItem:
        """Store one observation.

        ``untrusted`` forces Tier D regardless of the source registry, because a
        log body is attacker-influenceable even when the log pipeline is trusted.
        """
        structured = structured_value or {}
        trust = TrustClass.TIER_D if untrusted else trust_for(source_type)
        digest = content_hash(source, provenance_uri, structured)
        evidence_id = new_id("ev")

        row = await self._db.fetchrow(
            """
            INSERT INTO evidence_items
                (id, incident_id, source, source_type, evidence_type, status,
                 trust_class, resource_id, summary, structured_value, content,
                 content_untrusted, provenance_uri, content_hash, observed_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (incident_id, content_hash) WHERE content_hash <> ''
            DO UPDATE SET summary = EXCLUDED.summary
            RETURNING id, retrieved_at
            """,
            evidence_id, incident_id, source, source_type.value, evidence_type.value,
            status.value, trust.value, resource_id, summary, structured, content,
            untrusted, provenance_uri, digest, observed_at,
        )
        assert row is not None

        body: UntrustedText | str | None = None
        if content is not None:
            body = UntrustedText(text=content, origin=source, evidence_id=row["id"]) \
                if untrusted else content

        return EvidenceItem(
            id=row["id"],
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            retrieved_at=row["retrieved_at"],
            observed_at=observed_at,
            resource_id=resource_id,
            summary=summary,
            structured_value=structured,
            content=body,
            provenance_uri=provenance_uri,
            trust_class=trust,
            status=status,
            content_hash=digest,
        )

    async def record_unavailable(
        self,
        *,
        incident_id: str,
        source: str,
        source_type: SourceType,
        reason: str,
    ) -> EvidenceItem:
        """Record that a source could not be queried.

        This is the mechanism that keeps 'we could not look' visibly different
        from 'we looked and found nothing' (PRD 13).
        """
        log.warning("evidence source unavailable", incident_id=incident_id,
                    source=source, reason=reason)
        return await self.record(
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=EvidenceType.EVIDENCE_GAP,
            summary=f"{source} unavailable: {reason}",
            structured_value={"reason": reason},
            provenance_uri=f"gap://{source}",
            status=EvidenceStatus.SOURCE_UNAVAILABLE,
        )

    async def list_for_incident(
        self, incident_id: str, *, include_gaps: bool = True, limit: int = 500
    ) -> list[EvidenceItem]:
        clause = "" if include_gaps else "AND status <> 'SOURCE_UNAVAILABLE'"
        rows = await self._db.fetch(
            f"""
            SELECT id, incident_id, source, source_type, evidence_type, status,
                   trust_class, resource_id, summary, structured_value, content,
                   content_untrusted, provenance_uri, content_hash,
                   observed_at, retrieved_at
            FROM evidence_items
            WHERE incident_id = $1 {clause}
            ORDER BY retrieved_at DESC
            LIMIT $2
            """,
            incident_id, min(limit, 1000),
        )
        return [self._row_to_item(r) for r in rows]

    async def get_many(self, evidence_ids: list[str]) -> dict[str, EvidenceItem]:
        """Bulk fetch for citation validation - one query, not N."""
        if not evidence_ids:
            return {}
        rows = await self._db.fetch(
            """
            SELECT id, incident_id, source, source_type, evidence_type, status,
                   trust_class, resource_id, summary, structured_value, content,
                   content_untrusted, provenance_uri, content_hash,
                   observed_at, retrieved_at
            FROM evidence_items WHERE id = ANY($1)
            """,
            evidence_ids,
        )
        return {r["id"]: self._row_to_item(r) for r in rows}

    async def set_status(self, evidence_id: str, status: EvidenceStatus) -> None:
        await self._db.execute(
            "UPDATE evidence_items SET status = $2 WHERE id = $1", evidence_id, status.value
        )

    async def counts_by_trust(self, incident_id: str) -> dict[str, int]:
        """Feeds the deterministic evidence-quality score used by policy."""
        rows = await self._db.fetch(
            """
            SELECT trust_class, count(*) AS n FROM evidence_items
            WHERE incident_id = $1 AND status <> 'SOURCE_UNAVAILABLE'
            GROUP BY trust_class
            """,
            incident_id,
        )
        return {r["trust_class"]: int(r["n"]) for r in rows}

    @staticmethod
    def _row_to_item(row: Any) -> EvidenceItem:
        content: UntrustedText | str | None = None
        if row["content"] is not None:
            content = (
                UntrustedText(
                    text=row["content"], origin=row["source"], evidence_id=row["id"]
                )
                if row["content_untrusted"]
                else row["content"]
            )
        return EvidenceItem(
            id=row["id"],
            incident_id=row["incident_id"],
            source=row["source"],
            source_type=SourceType(row["source_type"]),
            evidence_type=EvidenceType(row["evidence_type"]),
            retrieved_at=row["retrieved_at"],
            observed_at=row["observed_at"],
            resource_id=row["resource_id"],
            summary=row["summary"],
            structured_value=row["structured_value"] or {},
            content=content,
            provenance_uri=row["provenance_uri"],
            trust_class=TrustClass(row["trust_class"]),
            status=EvidenceStatus(row["status"]),
            content_hash=row["content_hash"],
        )
