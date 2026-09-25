"""Knowledge tools: prior incidents, documents, and the code itself.

Read-only, and untrusted throughout. Everything this module returns - a runbook
passage, a commit message, a file at a ref, a diff hunk - is text some human or
some pipeline wrote, and every one of those is a place an instruction could be
planted for an agent to find. So every body is typed ``UntrustedText`` and the
registry enforces it.

Repository access is templated the same way telemetry is: a caller names an
``owner/repo``, a path and a ref, each validated by ``integrations.github``.
There is no search-query passthrough and no shell.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import Field

from aegis.core.errors import ExternalServiceError, SourceUnavailable, ValidationError
from aegis.domain.enums import EvidenceType, SourceType
from aegis.domain.models import UntrustedText
from aegis.integrations.github import CommitSummary as GitHubCommit
from aegis.mcp.deps import ToolDeps
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.tools import support
from aegis.mcp.types import (
    ENVIRONMENTS,
    ToolContext,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
    untrusted,
)
from aegis.retrieval.hybrid import RetrievalScope

MAX_CHUNKS = 20
MAX_COMMITS = 30
MAX_FILES = 40
MAX_SYMBOLS = 25
MAX_BLOB_BYTES = 64 * 1024

REPO_PATTERN = r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValidationError("repo must be 'owner/name'", context={"repo": repo[:80]})
    return owner, name


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class HybridSearchInput(ToolInput):
    query: str = Field(min_length=3, max_length=1_000)
    # A literal rather than the enum itself: arguments arrive as JSON from an
    # MCP client, and strict validation of an enum would reject the plain string
    # every wire format actually sends.
    scope: Literal["documents", "code", "all"] = "all"
    limit: int = Field(default=8, ge=1, le=MAX_CHUNKS)
    service_ids: list[str] = Field(default_factory=list, max_length=32)


class ChunkOut(ToolOutput):
    id: str
    kind: str
    score: float
    source: str
    citation: str
    provenance_uri: str
    # Corpus text is authored outside Aegis - a runbook, a postmortem, a source
    # file - so it is Tier D whatever the corpus it came from.
    snippet: UntrustedText


class HybridSearchOutput(ToolOutput):
    query: str
    scope: str
    signals_used: tuple[str, ...] = ()
    chunks: tuple[ChunkOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.chunks


class SimilarIncidentsInput(ToolInput):
    symptom: str = Field(min_length=3, max_length=2_000)
    services: list[str] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=5, ge=1, le=10)
    cause_category: str = Field(default="", max_length=120)


class MemoryMatchOut(ToolOutput):
    memory_id: str
    incident_id: str | None
    title: str
    # The confirmed cause of the prior incident, not merely its category. This
    # is the single most useful thing a precedent can tell the hypothesis step,
    # and summarising it away leaves the model to guess what "database" meant.
    root_cause: str
    cause_category: str
    confidence: float
    match_type: str
    reason: str
    occurrences: int
    provenance_uri: str
    shared_services: tuple[str, ...] = ()
    successful_fix: str = ""


class SimilarIncidentsOutput(ToolOutput):
    symptom: str
    matches: tuple[MemoryMatchOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.matches


class RecurringPatternsInput(ToolInput):
    window_days: int = Field(default=90, ge=1, le=365)
    min_occurrences: int = Field(default=2, ge=2, le=100)
    limit: int = Field(default=10, ge=1, le=50)


class RecurringPatternOut(ToolOutput):
    fingerprint: str
    title: str
    cause_category: str
    services: tuple[str, ...]
    occurrences: int
    first_seen: str | None = None
    last_seen: str | None = None
    prevention: str = ""


class RecurringPatternsOutput(ToolOutput):
    window_days: int
    patterns: tuple[RecurringPatternOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.patterns


class LocalizeCodeInput(ToolInput):
    symptom: str = Field(min_length=3, max_length=2_000)
    service_ids: list[str] = Field(min_length=1, max_length=16)
    lookback_hours: int = Field(default=48, ge=1, le=720)
    repo_hints: list[str] = Field(default_factory=list, max_length=8)


class FileCandidateOut(ToolOutput):
    repo: str
    ref: str
    path: str
    score: float
    reason: str
    provenance_uri: str


class SymbolCandidateOut(ToolOutput):
    repo: str
    ref: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    score: float
    reason: str
    provenance_uri: str
    snippet: UntrustedText


class LocalizeCodeOutput(ToolOutput):
    symptom_terms: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    files: tuple[FileCandidateOut, ...] = ()
    symbols: tuple[SymbolCandidateOut, ...] = ()
    tests: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.symbols


class ReadFileInput(ToolInput):
    repo: str = Field(pattern=REPO_PATTERN, max_length=140)
    path: str = Field(min_length=1, max_length=400)
    ref: str = Field(min_length=1, max_length=200)
    max_bytes: int = Field(default=16_384, ge=256, le=MAX_BLOB_BYTES)


class ReadFileOutput(ToolOutput):
    repo: str
    path: str
    ref: str
    bytes_read: int = 0
    truncated: bool = False
    provenance_uri: str = ""
    # Repository contents are human-authored free text. A file can contain a
    # comment addressed to a model as easily as it can contain code.
    text: UntrustedText | None = None

    @property
    def is_empty(self) -> bool:
        return self.text is None


class RecentCommitsInput(ToolInput):
    repo: str = Field(pattern=REPO_PATTERN, max_length=140)
    lookback_hours: int = Field(default=24, ge=1, le=720)
    path: str | None = Field(default=None, max_length=400)
    limit: int = Field(default=20, ge=1, le=MAX_COMMITS)


class CommitOut(ToolOutput):
    sha: str
    author: str
    authored_at: str | None
    url: str
    message: UntrustedText


class RecentCommitsOutput(ToolOutput):
    repo: str
    lookback_hours: int
    provenance_uri: str = ""
    commits: tuple[CommitOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.commits


class CompareRefsInput(ToolInput):
    repo: str = Field(pattern=REPO_PATTERN, max_length=140)
    base: str = Field(min_length=1, max_length=200)
    head: str = Field(min_length=1, max_length=200)


class ChangedFileOut(ToolOutput):
    filename: str
    status: str
    additions: int
    deletions: int
    patch_truncated: bool = False
    patch: UntrustedText | None = None


class CompareRefsOutput(ToolOutput):
    repo: str
    base: str
    head: str
    status: str = ""
    ahead_by: int = 0
    behind_by: int = 0
    provenance_uri: str = ""
    commits: tuple[CommitOut, ...] = ()
    files: tuple[ChangedFileOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.commits and not self.files


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def register(registry: ToolRegistry, deps: ToolDeps) -> None:
    """Declare the knowledge tools against an injected dependency set."""

    async def hybrid_search(context: ToolContext, args: HybridSearchInput) -> ToolOutcome:
        scope = RetrievalScope(args.scope)
        empty = HybridSearchOutput(query=args.query, scope=scope.value)
        if deps.retriever is None:
            return await support.degraded(
                deps, context, source="retrieval", source_type=SourceType.RUNBOOK,
                reason="hybrid retriever is not configured", value=empty,
            )
        try:
            result = await deps.retriever.search(
                args.query,
                scope=scope,
                limit=args.limit,
                incident_id=context.incident_id,
                service_ids=list(args.service_ids),
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="retrieval", source_type=SourceType.RUNBOOK,
                reason=exc.message, value=empty,
            )

        value = HybridSearchOutput(
            query=args.query, scope=scope.value,
            signals_used=result.signals_used,
            chunks=tuple(
                ChunkOut(
                    id=c.id, kind=c.kind, score=c.score, source=c.source,
                    citation=c.citation, provenance_uri=c.provenance_uri,
                    snippet=untrusted(c.content, origin=f"corpus:{c.kind}"),
                )
                for c in result.chunks[:MAX_CHUNKS]
            ),
        )
        if result.degraded:
            # The retriever ran but one of its signals could not: a partial
            # answer, reported as partial rather than as the whole truth.
            return await _partial(
                deps, context, value,
                source="retrieval", source_type=SourceType.RUNBOOK,
                reason=result.degraded_reason or "retrieval degraded",
            )
        if result.is_empty:
            return ToolOutcome(value=empty)

        ids = await support.record_evidence(
            deps, context, source="retrieval", source_type=SourceType.RUNBOOK,
            evidence_type=EvidenceType.CODE_SNIPPET
            if scope is RetrievalScope.CODE
            else EvidenceType.HISTORICAL_INCIDENT,
            summary=f"{len(result.chunks)} passages for {args.query[:120]!r}",
            structured_value={
                "query": args.query,
                "citations": [c.citation for c in result.chunks[:MAX_CHUNKS]],
                "signals": list(result.signals_used),
            },
            provenance_uri=result.chunks[0].provenance_uri,
        )
        return ToolOutcome(
            value=value, evidence_ids=ids,
            provenance=tuple(c.provenance_uri for c in result.chunks[:MAX_CHUNKS]),
        )

    async def similar_incidents(
        context: ToolContext, args: SimilarIncidentsInput
    ) -> ToolOutcome:
        empty = SimilarIncidentsOutput(symptom=args.symptom)
        if deps.memory is None:
            return await support.degraded(
                deps, context, source="incident_memory", source_type=SourceType.MEMORY,
                reason="incident memory is not configured", value=empty,
            )
        try:
            recall = await deps.memory.similar(
                args.symptom,
                services=list(args.services),
                limit=args.limit,
                cause_category=args.cause_category,
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="incident_memory", source_type=SourceType.MEMORY,
                reason=exc.message, value=empty,
            )

        value = SimilarIncidentsOutput(
            symptom=args.symptom,
            matches=tuple(
                MemoryMatchOut(
                    memory_id=m.memory.id, incident_id=m.memory.incident_id,
                    title=m.memory.title, root_cause=m.memory.root_cause,
                    cause_category=m.memory.cause_category,
                    confidence=m.confidence, match_type=m.match_type, reason=m.reason,
                    occurrences=m.memory.occurrences, provenance_uri=m.provenance_uri,
                    shared_services=m.shared_services,
                    successful_fix=m.memory.successful_fix,
                )
                for m in recall.matches
            ),
        )
        if recall.degraded:
            return await _partial(
                deps, context, value,
                source="incident_memory", source_type=SourceType.MEMORY,
                reason=recall.degraded_reason or "memory recall degraded",
            )
        if recall.is_empty:
            return ToolOutcome(value=empty)

        ids = await support.record_evidence(
            deps, context, source="incident_memory", source_type=SourceType.MEMORY,
            evidence_type=EvidenceType.HISTORICAL_INCIDENT,
            summary=f"{len(recall.matches)} prior incidents resemble this one",
            structured_value=value.model_dump(mode="json"),
            provenance_uri=recall.matches[0].provenance_uri,
        )
        return ToolOutcome(
            value=value, evidence_ids=ids,
            provenance=tuple(m.provenance_uri for m in recall.matches),
        )

    async def recurring_patterns(
        context: ToolContext, args: RecurringPatternsInput
    ) -> ToolOutcome:
        empty = RecurringPatternsOutput(window_days=args.window_days)
        if deps.memory is None:
            return await support.degraded(
                deps, context, source="incident_memory", source_type=SourceType.MEMORY,
                reason="incident memory is not configured", value=empty,
            )
        try:
            patterns = await deps.memory.recurring_patterns(
                args.window_days,
                min_occurrences=args.min_occurrences,
                limit=args.limit,
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="incident_memory", source_type=SourceType.MEMORY,
                reason=exc.message, value=empty,
            )
        if not patterns:
            return ToolOutcome(value=empty)
        value = RecurringPatternsOutput(
            window_days=args.window_days,
            patterns=tuple(
                RecurringPatternOut(
                    fingerprint=p.fingerprint, title=p.title,
                    cause_category=p.cause_category, services=p.services,
                    occurrences=p.occurrences,
                    first_seen=p.first_seen_at.isoformat() if p.first_seen_at else None,
                    last_seen=p.last_seen_at.isoformat() if p.last_seen_at else None,
                    prevention=p.prevention,
                )
                for p in patterns
            ),
        )
        return ToolOutcome(
            value=value, provenance=(f"memory://recurring?window_days={args.window_days}",)
        )

    async def localize_code(context: ToolContext, args: LocalizeCodeInput) -> ToolOutcome:
        empty = LocalizeCodeOutput()
        if deps.code is None:
            return await support.degraded(
                deps, context, source="code_retrieval", source_type=SourceType.VCS,
                reason="code retriever is not configured", value=empty,
            )
        if not context.incident_id:
            return await support.degraded(
                deps, context, source="code_retrieval", source_type=SourceType.VCS,
                reason="localize_code requires an incident context", value=empty,
            )
        since = deps.clock.now() - timedelta(hours=args.lookback_hours)
        try:
            found = await deps.code.localize(
                context.incident_id,
                list(args.service_ids),
                args.symptom,
                since=since,
                repo_hints=list(args.repo_hints),
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="code_retrieval", source_type=SourceType.VCS,
                reason=exc.message, value=empty,
            )

        value = LocalizeCodeOutput(
            symptom_terms=found.symptom_terms,
            repos=found.repos,
            files=tuple(
                FileCandidateOut(
                    repo=f.repo, ref=f.ref, path=f.path, score=f.score,
                    reason=f.reason, provenance_uri=f.provenance_uri,
                )
                for f in found.files[:MAX_FILES]
            ),
            symbols=tuple(
                SymbolCandidateOut(
                    repo=s.repo, ref=s.ref, path=s.path, symbol=s.symbol,
                    start_line=s.start_line, end_line=s.end_line, score=s.score,
                    reason=s.reason, provenance_uri=s.provenance_uri,
                    snippet=untrusted(s.snippet, origin="code"),
                )
                for s in found.symbols[:MAX_SYMBOLS]
            ),
            tests=tuple(f"{t.repo}/{t.path}" for t in found.tests[:MAX_FILES]),
        )
        if found.degraded:
            return await _partial(
                deps, context, value, source="code_retrieval",
                source_type=SourceType.VCS,
                reason=found.degraded_reason or "code localisation degraded",
            )
        if found.is_empty:
            return ToolOutcome(value=value)

        ids = await support.record_evidence(
            deps, context, source="code_retrieval", source_type=SourceType.VCS,
            evidence_type=EvidenceType.CODE_SNIPPET,
            summary=(
                f"{len(found.files)} files and {len(found.symbols)} symbols localised "
                f"for {args.symptom[:100]!r}"
            ),
            structured_value={
                "repos": list(found.repos),
                "files": [f.path for f in found.files[:MAX_FILES]],
                "symbols": [f"{s.path}:{s.symbol}" for s in found.symbols[:MAX_SYMBOLS]],
            },
            provenance_uri=(
                found.symbols[0].provenance_uri if found.symbols
                else found.files[0].provenance_uri
            ),
        )
        return ToolOutcome(value=value, evidence_ids=ids)

    # ---- version control -------------------------------------------------- #

    async def read_file_at_ref(context: ToolContext, args: ReadFileInput) -> ToolOutcome:
        empty = ReadFileOutput(repo=args.repo, path=args.path, ref=args.ref)
        if deps.github is None:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason="github client is not configured", value=empty,
            )
        owner, name = _split_repo(args.repo)
        uri = f"github://{args.repo}/blob/{args.ref}/{args.path}"
        try:
            blob = await deps.github.file_at_ref(
                owner, name, args.path, args.ref, max_bytes=args.max_bytes
            )
        except ExternalServiceError as exc:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason=exc.message, value=empty,
            )
        value = ReadFileOutput(
            repo=args.repo, path=blob.path, ref=blob.ref,
            bytes_read=blob.bytes_read, truncated=blob.truncated,
            provenance_uri=uri,
            text=untrusted(blob.text, origin="repository_file"),
        )
        ids = await support.record_evidence(
            deps, context, source="github", source_type=SourceType.VCS,
            evidence_type=EvidenceType.CODE_SNIPPET,
            summary=f"{args.repo}/{blob.path}@{blob.ref} ({blob.bytes_read} bytes)",
            structured_value={
                "repo": args.repo, "path": blob.path, "ref": blob.ref,
                "bytes": blob.bytes_read, "truncated": blob.truncated,
            },
            provenance_uri=uri, content=blob.text, untrusted=True,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def recent_commits(context: ToolContext, args: RecentCommitsInput) -> ToolOutcome:
        empty = RecentCommitsOutput(repo=args.repo, lookback_hours=args.lookback_hours)
        if deps.github is None:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason="github client is not configured", value=empty,
            )
        owner, name = _split_repo(args.repo)
        since = deps.clock.now() - timedelta(hours=args.lookback_hours)
        uri = f"github://{args.repo}/commits?since={since.isoformat()}"
        try:
            commits = await deps.github.recent_commits(
                owner, name, since=since, path=args.path, limit=args.limit
            )
        except ExternalServiceError as exc:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason=exc.message, value=empty,
            )
        if not commits:
            return ToolOutcome(
                value=RecentCommitsOutput(
                    repo=args.repo, lookback_hours=args.lookback_hours, provenance_uri=uri
                ),
                provenance=(uri,),
            )
        value = RecentCommitsOutput(
            repo=args.repo, lookback_hours=args.lookback_hours, provenance_uri=uri,
            commits=tuple(_commit_out(c) for c in commits[:MAX_COMMITS]),
        )
        ids = await support.record_evidence(
            deps, context, source="github", source_type=SourceType.VCS,
            evidence_type=EvidenceType.CODE_CHANGE,
            summary=f"{len(commits)} commits in {args.repo} over {args.lookback_hours}h",
            structured_value={
                "repo": args.repo,
                "shas": [c.sha for c in commits[:MAX_COMMITS]],
                "authors": sorted({c.author for c in commits[:MAX_COMMITS]}),
            },
            provenance_uri=uri,
            # Commit messages are Tier D: the author writes whatever they like.
            content="\n".join(c.message.text for c in commits[:MAX_COMMITS]),
            untrusted=True,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def compare_refs(context: ToolContext, args: CompareRefsInput) -> ToolOutcome:
        empty = CompareRefsOutput(repo=args.repo, base=args.base, head=args.head)
        if deps.github is None:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason="github client is not configured", value=empty,
            )
        owner, name = _split_repo(args.repo)
        uri = f"github://{args.repo}/compare/{args.base}...{args.head}"
        try:
            diff = await deps.github.compare(owner, name, args.base, args.head)
        except ExternalServiceError as exc:
            return await support.degraded(
                deps, context, source="github", source_type=SourceType.VCS,
                reason=exc.message, value=empty,
            )
        value = CompareRefsOutput(
            repo=args.repo, base=diff.base, head=diff.head, status=diff.status,
            ahead_by=diff.ahead_by, behind_by=diff.behind_by, provenance_uri=uri,
            commits=tuple(_commit_out(c) for c in diff.commits[:MAX_COMMITS]),
            files=tuple(
                ChangedFileOut(
                    filename=f.filename, status=f.status, additions=f.additions,
                    deletions=f.deletions, patch_truncated=f.patch_truncated,
                    patch=untrusted(f.patch, origin="diff") if f.patch else None,
                )
                for f in diff.files[:MAX_FILES]
            ),
        )
        ids = await support.record_evidence(
            deps, context, source="github", source_type=SourceType.VCS,
            evidence_type=EvidenceType.CODE_CHANGE,
            summary=(
                f"{args.repo} {args.base}...{args.head}: {diff.ahead_by} ahead, "
                f"{len(diff.files)} files changed"
            ),
            structured_value={
                "repo": args.repo, "base": diff.base, "head": diff.head,
                "status": diff.status, "ahead_by": diff.ahead_by,
                "files": [f.filename for f in diff.files[:MAX_FILES]],
            },
            provenance_uri=uri,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    # ---- specs ----------------------------------------------------------- #

    registry.register(
        ToolSpec(
            name="hybrid_search",
            description=(
                "Lexical + vector + graph + recency search over runbooks, postmortems "
                "and code. Passages come back as UntrustedText with citations."
            ),
            server="knowledge",
            input_model=HybridSearchInput,
            output_model=HybridSearchOutput,
            access="read",
            mutates="nothing",
            scope="knowledge:search",
            environments=ENVIRONMENTS,
            timeout_s=25.0,
            retryable=True,
            idempotent=True,
            cost_hint="moderate",
        ),
        hybrid_search,
    )
    registry.register(
        ToolSpec(
            name="similar_incidents",
            description="Prior incidents that resemble this one, with why and how strongly.",
            server="knowledge",
            input_model=SimilarIncidentsInput,
            output_model=SimilarIncidentsOutput,
            access="read",
            mutates="nothing",
            scope="memory:read",
            environments=ENVIRONMENTS,
            timeout_s=20.0,
            retryable=True,
            idempotent=True,
            cost_hint="moderate",
        ),
        similar_incidents,
    )
    registry.register(
        ToolSpec(
            name="recurring_patterns",
            description="Repeated failure modes inside a window, from approved memories.",
            server="knowledge",
            input_model=RecurringPatternsInput,
            output_model=RecurringPatternsOutput,
            access="read",
            mutates="nothing",
            scope="memory:read",
            environments=ENVIRONMENTS,
            timeout_s=20.0,
            retryable=True,
            idempotent=True,
            cost_hint="cheap",
        ),
        recurring_patterns,
    )
    registry.register(
        ToolSpec(
            name="localize_code",
            description=(
                "Narrow a symptom to repositories, files and symbols for the services "
                "already implicated."
            ),
            server="knowledge",
            input_model=LocalizeCodeInput,
            output_model=LocalizeCodeOutput,
            access="read",
            mutates="nothing",
            scope="code:read",
            environments=ENVIRONMENTS,
            timeout_s=60.0,
            retryable=True,
            idempotent=True,
            cost_hint="expensive",
        ),
        localize_code,
    )
    registry.register(
        ToolSpec(
            name="read_file_at_ref",
            description="Read a bounded slice of one file at one ref. Content is Tier D.",
            server="knowledge",
            input_model=ReadFileInput,
            output_model=ReadFileOutput,
            access="read",
            mutates="nothing",
            scope="code:read",
            environments=ENVIRONMENTS,
            timeout_s=20.0,
            retryable=True,
            idempotent=True,
            cost_hint="cheap",
        ),
        read_file_at_ref,
    )
    registry.register(
        ToolSpec(
            name="recent_commits",
            description="Commits in a repository over a bounded lookback window.",
            server="knowledge",
            input_model=RecentCommitsInput,
            output_model=RecentCommitsOutput,
            access="read",
            mutates="nothing",
            scope="code:read",
            environments=ENVIRONMENTS,
            timeout_s=25.0,
            retryable=True,
            idempotent=True,
            cost_hint="moderate",
        ),
        recent_commits,
    )
    registry.register(
        ToolSpec(
            name="compare_refs",
            description="Diff two refs: commits, changed files and bounded patches.",
            server="knowledge",
            input_model=CompareRefsInput,
            output_model=CompareRefsOutput,
            access="read",
            mutates="nothing",
            scope="code:read",
            environments=ENVIRONMENTS,
            timeout_s=30.0,
            retryable=True,
            idempotent=True,
            cost_hint="moderate",
        ),
        compare_refs,
    )


def _commit_out(commit: GitHubCommit) -> CommitOut:
    """Both GitHub read paths return the same commit shape."""
    return CommitOut(
        sha=commit.sha,
        author=commit.author,
        authored_at=commit.authored_at.isoformat() if commit.authored_at else None,
        url=commit.url,
        # Already Tier D at the integration boundary; carried, never unwrapped.
        message=commit.message,
    )


async def _partial(
    deps: ToolDeps,
    context: ToolContext,
    value: ToolOutput,
    *,
    source: str,
    source_type: SourceType,
    reason: str,
) -> ToolOutcome:
    """A real but incomplete answer: keep the rows, keep the gap.

    Distinct from ``support.degraded``, which returns nothing at all. Here the
    tool did find something, and discarding it would be as misleading as hiding
    the gap.
    """
    gap = await support.degraded(
        deps, context, source=source, source_type=source_type, reason=reason, value=value
    )
    return ToolOutcome(
        value=value,
        evidence_ids=gap.evidence_ids,
        provenance=gap.provenance,
        degraded=True,
        degraded_reason=reason,
    )


__all__ = ["register"]
