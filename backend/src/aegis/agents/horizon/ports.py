"""The seams between the horizon orchestrator and everything it drives.

The orchestrator owns control flow and nothing else. The brain, the compactor's
language model, RawTree, Nimble, FLUX and the checkpoint store all sit behind
the Protocols here, and every one of them has a fallback that satisfies the
same Protocol. That is what lets the golden path run in CI with zero network:
the scripted brain, the rule compactor, an in-memory store and fixture-backed
sponsors are all first-class implementations, not mocks.

Every result type carries a ``source`` so the UI can say which path ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from aegis.domain.horizon import (
    EvidenceCard,
    HorizonEvent,
    HorizonState,
    MemoryCard,
    Source,
)

# --------------------------------------------------------------------------- #
# brain                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the brain for one step.

    ``input_schema`` is a JSON Schema object. Providers translate it into their
    own dialect (Anthropic ``tools`` with ``strict: true``; Gemini function
    declarations); the orchestrator never sees a provider dialect.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call in the provider-neutral shape.

    ``arguments`` is always the parsed JSON object (``json.loads`` of the
    provider's serialised input); nothing downstream string-matches raw input.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrainRequest:
    """Exactly one fresh request per step: no prior turns, ever.

    ``system`` and ``tools`` are the stable, cacheable prefix. ``user`` is the
    single user turn holding [STATE] [EVIDENCE] [MEMORY] [PHASE] [ASK].
    """

    system: str
    user: str
    tools: tuple[ToolSpec, ...]
    phase: str
    step: int
    max_tool_calls: int = 4
    high_effort: bool = False  # diagnose / plan steps


@dataclass(frozen=True, slots=True)
class BrainDecision:
    tool_calls: tuple[ToolCall, ...]
    source: Source  # bedrock | gemini | scripted
    model: str = ""
    text: str = ""  # free text the model emitted; logged, never executed
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    fallback_reason: str | None = None  # set when a higher tier was skipped/failed


@runtime_checkable
class Brain(Protocol):
    """Chooses tools for one step. Never raises for provider failure.

    The router tries Bedrock, then Gemini, then the scripted policy; the
    scripted policy cannot fail. A decision always comes back.
    """

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision: ...

    def status(self) -> dict[str, Any]:
        """Per-tier readiness for /health and the war-room badge. No secrets."""
        ...


# --------------------------------------------------------------------------- #
# compactor                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CompactionInput:
    evidence_id: str
    step: int
    tool: str
    origin: Source
    raw: str  # already wrapped/serialised; treated as untrusted data
    structured: dict[str, Any] | None = None
    url: str | None = None
    hypothesis_ids: tuple[str, ...] = ()


class CompactorLLM(Protocol):
    """Unstructured text -> card fields, via Gemini's compactor key pool.

    Returns ``None`` when every compactor key is throttled or the output fails
    validation after one retry; the caller then uses the rule compactor. It
    never waits on a quota.
    """

    @property
    def configured(self) -> bool: ...

    async def compact(self, item: CompactionInput) -> dict[str, Any] | None:
        """Return ``{"claim", "supports", "refutes", "weight"}`` or ``None``."""
        ...


# --------------------------------------------------------------------------- #
# RawTree                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QueryResult:
    name: str  # named query, or "agent" for an MCP run-query
    sql: str
    rows: list[dict[str, Any]]
    source: Source  # rawtree | postgres | zscore
    duration_ms: int = 0
    error: str | None = None


class RawTreePort(Protocol):
    """Write path (write key) and named-query read path (read key).

    Writes are enqueued and batched; ``enqueue_*`` never blocks and never
    raises. Metrics may be dropped under pressure (counted); events,
    observations and memory cards are never dropped silently because Postgres
    holds them regardless.
    """

    @property
    def write_configured(self) -> bool: ...

    @property
    def read_configured(self) -> bool: ...

    def enqueue_metrics(self, rows: list[dict[str, Any]]) -> None: ...

    def enqueue_event(self, event: HorizonEvent) -> None: ...

    def enqueue_observation(
        self, *, incident_id: str, evidence_id: str, tool: str, raw: str, ts: datetime
    ) -> None: ...

    def enqueue_memory_card(self, card: MemoryCard) -> None: ...

    async def named_query(self, name: str, params: dict[str, Any]) -> QueryResult:
        """Run one of the named queries in code. Unknown names raise."""
        ...

    def stats(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# external evidence (Nimble)                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KnownIssue:
    title: str
    url: str
    excerpt: str  # untrusted page text, bounded
    component: str
    version: str


@dataclass(frozen=True, slots=True)
class KnownIssueResult:
    issues: list[KnownIssue]
    source: Source  # nimble | fixture
    query: str
    reason: str = ""  # why the fixture was used, when it was
    duration_ms: int = 0


class RemoteToolset(Protocol):
    """A remote MCP server's tools, already filtered to an allowlist.

    RawTree's agent connection uses the read key and exposes only
    ``run-query``, ``list-tables`` and ``describe-table``; anything else the
    server advertises (``delete-table``, ``create-api-key`` ...) is removed
    before the brain ever sees the list, and calling it raises.
    """

    @property
    def available(self) -> bool: ...

    def tool_specs(self) -> list[ToolSpec]:
        """Allowlisted tools, names prefixed (e.g. ``rawtree__run-query``)."""
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> QueryResult: ...


class KnownIssueSearch(Protocol):
    async def search_known_issues(self, component: str, version: str) -> KnownIssueResult:
        """Never raises: a failure or 8 s timeout returns the fixture."""
        ...


# --------------------------------------------------------------------------- #
# incident map (FLUX)                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IncidentMapResult:
    status: str  # ready | unavailable
    source: Source  # flux | system
    image_url: str | None = None  # path served by the API once stored
    image_bytes: bytes | None = None  # downloaded promptly: BFL result URLs expire
    mime: str = "image/jpeg"
    reason: str = ""
    prompt: str = ""


class IncidentMapRenderer(Protocol):
    async def render(self, state: HorizonState, card: MemoryCard) -> IncidentMapResult:
        """Bounded (60 s), one image per incident, no retry on submit."""
        ...


# --------------------------------------------------------------------------- #
# checkpoint store and event sink                                              #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StoredObservation:
    evidence_id: str
    incident_id: str
    tool: str
    raw: str
    card: EvidenceCard | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class HorizonStore(Protocol):
    """Postgres-backed in production, in-memory in unit tests.

    ``save_checkpoint`` is atomic per step: after it returns, a ``kill -9``
    resumes from exactly this ``(run_id, step, phase)``.
    """

    async def save_checkpoint(self, state: HorizonState) -> None: ...

    async def load_latest(self, incident_id: str) -> HorizonState | None: ...

    async def append_event(self, event: HorizonEvent) -> int:
        """Persist and return a monotonic sequence number."""
        ...

    async def events(
        self, incident_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, HorizonEvent]]: ...

    async def save_observation(self, obs: StoredObservation) -> None: ...

    async def get_observation(self, evidence_id: str) -> StoredObservation | None: ...

    async def save_memory_card(self, card: MemoryCard) -> None:
        """Insert or replace by ``card.id`` (the image fields change later)."""
        ...

    async def memory_cards(self, *, limit: int = 20) -> list[MemoryCard]: ...

    async def save_incident_map(self, card_id: str, image: bytes, mime: str) -> str:
        """Store the FLUX image; return the API path that serves it."""
        ...


class EventSink(Protocol):
    """Fan-out for live UI. Never raises; publishing is a convenience."""

    async def publish(self, seq: int, event: HorizonEvent) -> None: ...


__all__ = [
    "Brain",
    "BrainDecision",
    "BrainRequest",
    "CompactionInput",
    "CompactorLLM",
    "EventSink",
    "HorizonStore",
    "IncidentMapRenderer",
    "IncidentMapResult",
    "KnownIssue",
    "KnownIssueResult",
    "KnownIssueSearch",
    "QueryResult",
    "RawTreePort",
    "RemoteToolset",
    "StoredObservation",
    "ToolCall",
    "ToolSpec",
]
