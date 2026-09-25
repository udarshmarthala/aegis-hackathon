"""Embedding generation for the hybrid corpus.

One rule governs this module: an unconfigured or failing embedding provider is
reported, never papered over. Returning zero vectors would make every cosine
similarity identical and turn semantic search into a silent no-op that still
looks like it ran - the retrieval equivalent of confusing "found nothing" with
"could not look" (PRD 13).

Callers therefore check ``configured`` first and degrade to lexical-only search
explicitly, or catch ``SourceUnavailable`` and record an evidence gap.

Embeddings come from the same Google AI Studio keys the model router uses, and
fail over the same way - across keys, not vendors (``core.keyring``). Ingestion
is bursty and quota-hungry, so the key ring matters more here than anywhere
else: one exhausted free-tier key should cost a slot, not the corpus.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import ConfigError, ExternalServiceError, SourceUnavailable
from aegis.core.keyring import KeyFault, KeyRing, KeySlot, classify
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call

log = get_logger(__name__)

PROVIDER: Final = "google"

# Batch size is capped so one ingestion call cannot build a request larger than
# a provider will accept, and so a failure retries a bounded amount of work.
MAX_BATCH_SIZE: Final = 64
# Per-input cap. Embedding endpoints reject oversized inputs with a 400, which
# is not retryable; truncating at the boundary keeps ingestion moving.
MAX_INPUT_CHARS: Final = 8_000
# Concurrency into one provider. Above this, ingestion starves the incident
# path of worker slots for no throughput gain.
_BULKHEAD_LIMIT: Final = 4

_GOOGLE_BASE: Final = "https://generativelanguage.googleapis.com/v1beta"


class EmbeddingClient:
    """Embeds text with Google AI Studio, failing over across configured keys.

    Secrets are read through ``SecretStr.get_secret_value`` at construction and
    never stored in plain form beyond the per-request header dict; log lines and
    error contexts carry the key's label, never the key.
    """

    __slots__ = ("_settings", "_ring", "_client", "_bulkhead")

    def __init__(self, settings: Settings, clock: Clock = SYSTEM_CLOCK) -> None:
        self._settings = settings
        self._ring = KeyRing(settings, purpose="embeddings", clock=clock)
        self._client: httpx.AsyncClient | None = None
        self._bulkhead = Bulkhead("embeddings", _BULKHEAD_LIMIT)

    # ------------------------------------------------------------------ #
    # configuration                                                       #
    # ------------------------------------------------------------------ #

    @property
    def configured(self) -> bool:
        """False when no key is present.

        The retriever reads this before doing any work so that the degraded
        reason it reports names configuration, not a runtime failure.
        """
        return self._ring.configured

    @property
    def provider(self) -> str | None:
        return PROVIDER if self._ring.configured else None

    @property
    def dimension(self) -> int:
        return self._settings.llm_embedding_dim

    def key_status(self) -> list[dict[str, Any]]:
        """Per-key readiness for ``/health``. Contains no secret material."""
        return self._ring.status()

    # ------------------------------------------------------------------ #
    # transport                                                           #
    # ------------------------------------------------------------------ #

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.llm_request_timeout_s),
                # Bounded pool: an embedding backlog must not exhaust the
                # process file-descriptor budget the incident path also uses.
                limits=httpx.Limits(
                    max_connections=_BULKHEAD_LIMIT, max_keepalive_connections=2
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool. Called from graceful shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _endpoint(self) -> str:
        model = self._settings.llm_embedding_model
        return f"{_GOOGLE_BASE}/models/{model}:batchEmbedContents"

    @staticmethod
    def _headers(slot: KeySlot) -> dict[str, str]:
        # The key goes in a header, never in the query string, so it cannot leak
        # through proxy or access logs that record URLs.
        return {"x-goog-api-key": slot.secret}

    # ------------------------------------------------------------------ #
    # embedding                                                           #
    # ------------------------------------------------------------------ #

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, preserving order.

        Raises ``SourceUnavailable`` when no key is configured or every key
        fails, and ``ConfigError`` when the returned dimension does not match
        ``llm_embedding_dim`` - a mismatch would be rejected by the
        ``vector(1536)`` column anyway, and failing here names the real cause
        instead of surfacing a Postgres type error.
        """
        if not self._ring.configured:
            raise SourceUnavailable(
                "no embedding provider is configured",
                context={"setting": "GOOGLE_API_KEY"},
            )
        if not texts:
            return []

        prepared = [t[:MAX_INPUT_CHARS] for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(prepared), MAX_BATCH_SIZE):
            batch = prepared[start : start + MAX_BATCH_SIZE]
            out.extend(await self._embed_batch(batch))

        self._check_dimension(out)
        return out

    async def embed_one(self, text: str) -> list[float]:
        """Convenience for query-side embedding, which is always a single input."""
        vectors = await self.embed([text])
        if not vectors:
            raise SourceUnavailable(
                "embedding provider returned no vector",
                context={"provider": PROVIDER},
            )
        return vectors[0]

    def _check_dimension(self, vectors: list[list[float]]) -> None:
        expected = self._settings.llm_embedding_dim
        for vec in vectors:
            if len(vec) != expected:
                raise ConfigError(
                    "embedding dimension does not match llm_embedding_dim",
                    context={
                        "provider": PROVIDER,
                        "model": self._settings.llm_embedding_model,
                        "expected_dim": expected,
                        "returned_dim": len(vec),
                    },
                )

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """One batch, tried against each usable key in turn.

        A key-level fault parks that key and advances. A request-level fault
        stops immediately: a 400 for an oversized input or an unknown model
        would be returned identically by all four keys, and burning the ring on
        it would report an outage where the cause is configuration.
        """
        url = self._endpoint()
        payload = self._payload(batch)
        last: ExternalServiceError | None = None
        tried: list[str] = []

        for slot in self._ring.slots():
            tried.append(slot.label)

            async def _post(_slot: KeySlot = slot) -> list[list[float]]:
                try:
                    response = await self._http().post(
                        url, json=payload, headers=self._headers(_slot)
                    )
                except httpx.HTTPError as exc:
                    # Transport failures are retryable; guarded_call decides how
                    # many times before the breaker opens.
                    raise ExternalServiceError(
                        "embedding request failed",
                        context={"provider": PROVIDER, "error": type(exc).__name__},
                    ) from exc
                return self._parse(response)

            try:
                vectors = await guarded_call(
                    _post,
                    dependency=slot.dependency,
                    timeout_s=self._settings.llm_request_timeout_s,
                    attempts=self._settings.llm_max_retries + 1,
                    bulkhead=self._bulkhead,
                )
            except ExternalServiceError as exc:
                last = exc
                fault = classify(exc)
                log.warning(
                    "embedding call failed",
                    key=slot.label,
                    batch_size=len(batch),
                    fault=fault.value,
                    error=exc.code,
                )
                if fault is KeyFault.REQUEST:
                    break
                self._ring.park(slot, fault)
                continue

            self._ring.release(slot)
            return vectors

        # Everything the breaker, the timeout or the provider raised becomes one
        # typed state the retriever knows how to degrade on.
        raise SourceUnavailable(
            "every configured embedding key is unavailable",
            context={
                "provider": PROVIDER,
                "keys_tried": tried,
                "cause": last.code if last else "unknown",
            },
        ) from last

    def _payload(self, batch: list[str]) -> dict[str, Any]:
        model = self._settings.llm_embedding_model
        return {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                    # Mandatory, not an optimisation: gemini-embedding-001
                    # returns 3072 dimensions by default and the corpus columns
                    # are vector(1536). Truncated vectors are not unit-normalised,
                    # which does not matter here because retrieval ranks by
                    # cosine distance (`<=>`), and cosine ignores magnitude.
                    "outputDimensionality": self._settings.llm_embedding_dim,
                }
                for text in batch
            ]
        }

    def _parse(self, response: httpx.Response) -> list[list[float]]:
        if response.status_code >= 400:
            # 4xx from an embedding endpoint is a configuration or quota fault
            # and will not succeed on retry; 5xx will. Marking them apart keeps
            # the retry budget pointed at failures that can actually recover.
            retryable = response.status_code >= 500 or response.status_code == 429
            raise ExternalServiceError(
                f"embedding provider returned {response.status_code}",
                # ``status`` is read back by ``keyring.classify``, which is how a
                # 429 on one key becomes "park and advance" rather than a guess
                # made by matching on an error message.
                context={"provider": PROVIDER, "status": response.status_code},
                retryable=retryable,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "embedding provider returned non-JSON",
                context={"provider": PROVIDER},
            ) from exc

        try:
            return [[float(x) for x in item["values"]] for item in body["embeddings"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "embedding provider returned an unexpected shape",
                context={"provider": PROVIDER},
            ) from exc


__all__ = ["MAX_BATCH_SIZE", "MAX_INPUT_CHARS", "PROVIDER", "EmbeddingClient"]
