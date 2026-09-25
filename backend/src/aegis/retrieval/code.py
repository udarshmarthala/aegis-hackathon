"""Hierarchical code localisation.

The product claim is "here is the function that broke", and the only way to
make that claim honestly is to narrow before reading, never after:

    incident -> affected services -> repositories -> commits in the incident
    window -> files those commits touched -> symbols in those files matching
    the symptom -> tests covering those symbols

Every stage is capped, and every stage records what it received and what it
emitted. Handing a model a whole repository and asking it to find the bug is
both unaffordable and unfalsifiable: there is no way to audit which narrowing
step was wrong. A recorded stage trail makes a localisation failure diagnosable
(ESD FailureClass.CODE_LOCALIZATION_FAILURE).

Nothing here loads a repository into memory. Content comes from
``code_documents`` chunks that ingestion already bounded, and commit metadata
comes from an injected ``CommitSource`` - not from a clone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import ExternalServiceError, SourceUnavailable, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceType, SourceType
from aegis.evidence.store import EvidenceStore
from aegis.persistence.db import Database
from aegis.retrieval.hybrid import HybridRetriever, RetrievalScope, code_provenance_uri

log = get_logger(__name__)

# Stage caps. These are the whole point of the module: each one is the maximum
# amount of the world that survives into the next stage.
MAX_SERVICES: Final = 12
MAX_REPOS: Final = 8
MAX_COMMITS: Final = 50
MAX_FILES: Final = 40
MAX_SYMBOLS: Final = 25
MAX_TESTS: Final = 15
MAX_SYMPTOM_TERMS: Final = 12
# Cap on the snippet text attached to evidence. The full chunk is retrievable
# by id; the evidence row exists to be read by a human on an incident page.
MAX_SNIPPET_CHARS: Final = 4_000

_TERM_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
# Words that appear in every alert and therefore discriminate nothing. Keeping
# them would make the symptom query match the entire corpus.
_STOPWORDS: Final = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "has", "have", "was",
        "were", "error", "errors", "failed", "failure", "exception", "warning",
        "service", "request", "requests", "response", "high", "low", "increase",
        "increased", "alert", "incident", "issue", "problem", "seen", "after",
    }
)


# --------------------------------------------------------------------------- #
# commit source contract                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CommitRecord:
    """Commit metadata, as the VCS integration supplies it.

    ``message`` is Tier-D free text authored by whoever pushed. It is carried
    here for display and matching, and is written to evidence inside an
    untrusted envelope - never concatenated into a prompt as plain text.
    """

    repo: str
    sha: str
    authored_at: datetime
    message: str
    author: str = ""
    files: tuple[str, ...] = ()
    url: str = ""

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@runtime_checkable
class CommitSource(Protocol):
    """What ``integrations`` must provide for the commit stage to run.

    Declared as a protocol so this module has no import dependency on the VCS
    integration: a GitHub outage degrades code localisation to a corpus search
    rather than breaking the import graph.
    """

    async def commits_in_window(
        self, repo: str, *, since: datetime, until: datetime, limit: int
    ) -> Sequence[CommitRecord]:
        """Commits authored in [since, until], newest first, at most ``limit``."""
        ...


# --------------------------------------------------------------------------- #
# results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StageTrace:
    """One narrowing step, recorded so a bad localisation can be attributed."""

    name: str
    received: int
    emitted: int
    cap: int
    note: str = ""

    @property
    def truncated(self) -> bool:
        return self.emitted >= self.cap


@dataclass(frozen=True, slots=True)
class FileCandidate:
    repo: str
    ref: str
    path: str
    score: float
    reason: str
    commit_shas: tuple[str, ...] = ()
    evidence_trail: tuple[str, ...] = ()

    @property
    def provenance_uri(self) -> str:
        return code_provenance_uri(
            repo=self.repo, ref=self.ref, path=self.path, start_line=0, end_line=0
        )


@dataclass(frozen=True, slots=True)
class SymbolCandidate:
    chunk_id: str
    repo: str
    ref: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    score: float
    reason: str
    snippet: str
    matched_terms: tuple[str, ...] = ()
    commit_shas: tuple[str, ...] = ()
    evidence_trail: tuple[str, ...] = ()

    @property
    def provenance_uri(self) -> str:
        return code_provenance_uri(
            repo=self.repo,
            ref=self.ref,
            path=self.path,
            start_line=self.start_line,
            end_line=self.end_line,
        )


@dataclass(frozen=True, slots=True)
class TestCandidate:
    repo: str
    ref: str
    path: str
    start_line: int
    end_line: int
    covers: tuple[str, ...]
    reason: str

    @property
    def provenance_uri(self) -> str:
        return code_provenance_uri(
            repo=self.repo,
            ref=self.ref,
            path=self.path,
            start_line=self.start_line,
            end_line=self.end_line,
        )


@dataclass(frozen=True, slots=True)
class CodeLocalization:
    """Ranked code candidates plus the audit trail of how they were reached."""

    incident_id: str
    service_ids: tuple[str, ...]
    symptom_terms: tuple[str, ...]
    repos: tuple[str, ...] = ()
    commits: tuple[CommitRecord, ...] = ()
    files: tuple[FileCandidate, ...] = ()
    symbols: tuple[SymbolCandidate, ...] = ()
    tests: tuple[TestCandidate, ...] = ()
    stages: tuple[StageTrace, ...] = ()
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def is_empty(self) -> bool:
        """Ran and found nothing - not the same as could-not-look."""
        return not self.files and not self.symbols

    def stage(self, name: str) -> StageTrace | None:
        return next((s for s in self.stages if s.name == name), None)


# --------------------------------------------------------------------------- #
# retriever                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class _Narrowing:
    """Mutable accumulator used while stages run; frozen into the result."""

    stages: list[StageTrace] = field(default_factory=list)
    degraded: bool = False
    reasons: list[str] = field(default_factory=list)

    def record(self, name: str, received: int, emitted: int, cap: int, note: str = "") -> None:
        self.stages.append(
            StageTrace(name=name, received=received, emitted=emitted, cap=cap, note=note)
        )

    def degrade(self, reason: str) -> None:
        self.degraded = True
        if reason not in self.reasons:
            self.reasons.append(reason)


class CodeRetriever:
    """Narrows an incident down to specific files, symbols and tests."""

    __slots__ = ("_db", "_retriever", "_commits", "_clock")

    def __init__(
        self,
        db: Database,
        retriever: HybridRetriever,
        *,
        commit_source: CommitSource | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._retriever = retriever
        self._commits = commit_source
        self._clock = clock

    async def localize(
        self,
        incident_id: str,
        service_ids: Sequence[str],
        symptom: str,
        *,
        since: datetime,
        repo_hints: Sequence[str] = (),
        until: datetime | None = None,
    ) -> CodeLocalization:
        """Localise ``symptom`` to code for the services already implicated."""
        if not incident_id:
            raise ValidationError("incident_id is required for code localisation")
        if not symptom.strip():
            raise ValidationError(
                "a symptom description is required", context={"incident_id": incident_id}
            )

        narrowing = _Narrowing()
        services = [s for s in dict.fromkeys(service_ids) if s][:MAX_SERVICES]
        narrowing.record("services", len(service_ids), len(services), MAX_SERVICES)

        terms = symptom_terms(symptom)
        window_end = until or self._clock.now()

        repos = await self._repositories(services, repo_hints, narrowing)
        if not repos:
            # No mapping means no narrowing is possible; saying so is far more
            # useful than searching every repo and returning noise.
            narrowing.degrade("no_repository_mapping_for_services")
            return self._finish(incident_id, services, terms, narrowing)

        commits = await self._commits_in_window(repos, since, window_end, narrowing)
        files = await self._files(repos, commits, symptom, services, narrowing)
        symbols = await self._symbols(files, terms, narrowing)
        tests = await self._tests(repos, symbols, narrowing)

        return self._finish(
            incident_id,
            services,
            terms,
            narrowing,
            repos=repos,
            commits=commits,
            files=files,
            symbols=symbols,
            tests=tests,
        )

    # -- stage 2: service -> repository -------------------------------- #

    async def _repositories(
        self, services: Sequence[str], repo_hints: Sequence[str], narrowing: _Narrowing
    ) -> list[str]:
        mapped: list[str] = []
        if services:
            rows = await self._db.fetch(
                """
                SELECT DISTINCT ON (repo) repo, rank
                FROM service_repositories
                WHERE service_id = ANY($1::text[])
                ORDER BY repo, rank
                LIMIT $2
                """,
                list(services),
                MAX_REPOS,
            )
            mapped = [r["repo"] for r in sorted(rows, key=lambda r: (r["rank"], r["repo"]))]

        # Hints come from an operator or a prior investigation step and take
        # precedence; they are still subject to the same cap.
        ordered = list(dict.fromkeys([*repo_hints, *mapped]))[:MAX_REPOS]
        narrowing.record(
            "repositories",
            len(services),
            len(ordered),
            MAX_REPOS,
            note="hints_applied" if repo_hints else "",
        )
        return ordered

    # -- stage 3: repository -> commits in the window ------------------ #

    async def _commits_in_window(
        self, repos: Sequence[str], since: datetime, until: datetime, narrowing: _Narrowing
    ) -> list[CommitRecord]:
        if self._commits is None:
            narrowing.degrade("no_commit_source_configured")
            narrowing.record("commits", len(repos), 0, MAX_COMMITS, note="source_not_configured")
            return []

        budget = max(MAX_COMMITS // max(len(repos), 1), 1)
        found: list[CommitRecord] = []
        for repo in repos:
            if len(found) >= MAX_COMMITS:
                break
            try:
                batch = await self._commits.commits_in_window(
                    repo, since=since, until=until, limit=budget
                )
            except (SourceUnavailable, ExternalServiceError) as exc:
                # One unreachable repository must not discard the commits we did
                # get from the others, but it does make the answer incomplete.
                narrowing.degrade(f"commit_source_unavailable:{repo}")
                log.warning("commit source unavailable", repo=repo, error=exc.code)
                continue
            found.extend(batch[: MAX_COMMITS - len(found)])

        found.sort(key=lambda c: c.authored_at, reverse=True)
        narrowing.record("commits", len(repos), len(found), MAX_COMMITS)
        return found

    # -- stage 4: commits -> files ------------------------------------- #

    async def _files(
        self,
        repos: Sequence[str],
        commits: Sequence[CommitRecord],
        symptom: str,
        services: Sequence[str],
        narrowing: _Narrowing,
    ) -> list[FileCandidate]:
        """Files the window's commits touched, ranked by how many touched them.

        A file changed by several commits inside the incident window is a
        stronger candidate than one changed once: churn immediately before an
        incident is the single most predictive change signal available.
        """
        touched: dict[tuple[str, str], list[CommitRecord]] = {}
        for commit in commits:
            for path in commit.files:
                touched.setdefault((commit.repo, path), []).append(commit)

        if touched:
            ranked = sorted(
                touched.items(),
                key=lambda kv: (-len(kv[1]), -kv[1][0].authored_at.timestamp(), kv[0]),
            )[:MAX_FILES]
            files = [
                FileCandidate(
                    repo=repo,
                    ref=hits[0].sha,
                    path=path,
                    score=float(len(hits)),
                    reason=(
                        f"touched by {len(hits)} commit(s) in the incident window, "
                        f"most recently {hits[0].short_sha}"
                    ),
                    commit_shas=tuple(c.sha for c in hits[:5]),
                    evidence_trail=("services", "repositories", "commits"),
                )
                for (repo, path), hits in ranked
            ]
            narrowing.record("files", len(commits), len(files), MAX_FILES, note="from_commits")
            return files

        # No commits in the window is a real and common outcome: not every
        # incident is a deploy. Fall back to the indexed corpus, scoped to the
        # mapped repositories, and say so - a corpus hit is weaker evidence than
        # a change inside the window and must not be presented as equal.
        files = await self._files_from_corpus(repos, symptom, services)
        narrowing.record("files", len(commits), len(files), MAX_FILES, note="from_corpus")
        if files:
            narrowing.degrade("no_commits_in_window_used_corpus_search")
        return files

    async def _files_from_corpus(
        self, repos: Sequence[str], symptom: str, services: Sequence[str]
    ) -> list[FileCandidate]:
        result = await self._retriever.search(
            symptom[:500],
            scope=RetrievalScope.CODE,
            limit=MAX_FILES,
            service_ids=list(services),
        )
        best: dict[tuple[str, str], FileCandidate] = {}
        for chunk in result:
            if chunk.repo not in repos:
                continue
            key = (chunk.repo, chunk.path)
            if key in best:
                continue
            best[key] = FileCandidate(
                repo=chunk.repo,
                ref=chunk.ref,
                path=chunk.path,
                score=chunk.score,
                reason="indexed content matches the symptom; no commit in the incident window",
                evidence_trail=("services", "repositories", "corpus_search"),
            )
        return list(best.values())[:MAX_FILES]

    # -- stage 5: files -> symbols ------------------------------------- #

    async def _symbols(
        self, files: Sequence[FileCandidate], terms: Sequence[str], narrowing: _Narrowing
    ) -> list[SymbolCandidate]:
        if not files or not terms:
            narrowing.record(
                "symbols",
                len(files),
                0,
                MAX_SYMBOLS,
                note="no_symptom_terms" if files else "no_candidate_files",
            )
            return []

        repos = list({f.repo for f in files})
        paths = list({f.path for f in files})
        query = " ".join(terms)
        by_path = {(f.repo, f.path): f for f in files}

        rows = await self._db.fetch(
            """
            SELECT id, repo, ref, path, coalesce(symbol, '') AS symbol,
                   start_line, end_line, content,
                   ts_rank_cd(tsv, q) AS signal
            FROM code_documents, plainto_tsquery('english', $1) q
            WHERE repo = ANY($2::text[]) AND path = ANY($3::text[]) AND tsv @@ q
            ORDER BY signal DESC, repo, path, start_line
            LIMIT $4
            """,
            query,
            repos,
            paths,
            MAX_SYMBOLS,
        )

        out: list[SymbolCandidate] = []
        for row in rows:
            parent = by_path.get((row["repo"], row["path"]))
            lowered = (row["content"] or "").lower()
            matched = tuple(t for t in terms if t.lower() in lowered)[:MAX_SYMPTOM_TERMS]
            out.append(
                SymbolCandidate(
                    chunk_id=row["id"],
                    repo=row["repo"],
                    # Prefer the commit the window implicated over the ref the
                    # chunk was indexed at, so the citation and the change agree.
                    ref=(parent.ref if parent and parent.commit_shas else row["ref"]),
                    path=row["path"],
                    symbol=row["symbol"],
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    score=float(row["signal"] or 0.0),
                    reason=(
                        f"symbol matches symptom terms {', '.join(matched) or '(none literal)'} "
                        f"in a file selected by {parent.reason if parent else 'corpus search'}"
                    ),
                    snippet=(row["content"] or "")[:MAX_SNIPPET_CHARS],
                    matched_terms=matched,
                    commit_shas=parent.commit_shas if parent else (),
                    evidence_trail=(
                        (*parent.evidence_trail, "symbols") if parent else ("symbols",)
                    ),
                )
            )
        narrowing.record("symbols", len(files), len(out), MAX_SYMBOLS)
        return out

    # -- stage 6: symbols -> covering tests ---------------------------- #

    async def _tests(
        self,
        repos: Sequence[str],
        symbols: Sequence[SymbolCandidate],
        narrowing: _Narrowing,
    ) -> list[TestCandidate]:
        """Tests that name the candidate symbols.

        Lexical rather than coverage-derived: a coverage database is not
        available for every workload, and a test that mentions the symbol is a
        sound starting point for reproduction. The reason string says which,
        so nobody mistakes this for real coverage data.
        """
        names = [s.symbol for s in symbols if s.symbol][:MAX_SYMBOLS]
        if not names or not repos:
            narrowing.record("tests", len(symbols), 0, MAX_TESTS, note="no_named_symbols")
            return []

        rows = await self._db.fetch(
            """
            SELECT repo, ref, path, start_line, end_line, content,
                   ts_rank_cd(tsv, q) AS signal
            FROM code_documents, plainto_tsquery('english', $1) q
            WHERE kind = 'test' AND repo = ANY($2::text[]) AND tsv @@ q
            ORDER BY signal DESC, repo, path, start_line
            LIMIT $3
            """,
            " ".join(names),
            list(repos),
            MAX_TESTS,
        )

        out: list[TestCandidate] = []
        for row in rows:
            body = row["content"] or ""
            covers = tuple(n for n in names if n in body)
            out.append(
                TestCandidate(
                    repo=row["repo"],
                    ref=row["ref"],
                    path=row["path"],
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    covers=covers,
                    reason=(
                        "test file references "
                        f"{', '.join(covers) if covers else 'the candidate symbols'} "
                        "(lexical association, not coverage data)"
                    ),
                )
            )
        narrowing.record("tests", len(symbols), len(out), MAX_TESTS)
        return out

    # -- assembly ------------------------------------------------------ #

    @staticmethod
    def _finish(
        incident_id: str,
        services: Sequence[str],
        terms: Sequence[str],
        narrowing: _Narrowing,
        *,
        repos: Sequence[str] = (),
        commits: Sequence[CommitRecord] = (),
        files: Sequence[FileCandidate] = (),
        symbols: Sequence[SymbolCandidate] = (),
        tests: Sequence[TestCandidate] = (),
    ) -> CodeLocalization:
        return CodeLocalization(
            incident_id=incident_id,
            service_ids=tuple(services),
            symptom_terms=tuple(terms),
            repos=tuple(repos),
            commits=tuple(commits),
            files=tuple(files),
            symbols=tuple(symbols),
            tests=tuple(tests),
            stages=tuple(narrowing.stages),
            degraded=narrowing.degraded,
            degraded_reason="; ".join(narrowing.reasons),
        )

    # ------------------------------------------------------------------ #
    # evidence                                                            #
    # ------------------------------------------------------------------ #

    async def to_evidence(
        self,
        evidence_store: EvidenceStore,
        incident_id: str,
        localization: CodeLocalization,
        *,
        max_commits: int = 10,
        max_symbols: int = 10,
    ) -> list[str]:
        """Write the localisation into the evidence store, returning its ids.

        A localisation that is never written as evidence cannot be cited by a
        diagnosis, and an uncited claim is rejected by the validator - so this
        is not a convenience, it is the step that makes the result usable.
        """
        recorded: list[str] = []

        for commit in localization.commits[:max_commits]:
            item = await evidence_store.record(
                incident_id=incident_id,
                source=f"vcs:{commit.repo}",
                source_type=SourceType.VCS,
                evidence_type=EvidenceType.CODE_CHANGE,
                summary=f"{commit.short_sha} touched {len(commit.files)} file(s) in the window",
                structured_value={
                    "repo": commit.repo,
                    "sha": commit.sha,
                    "author": commit.author,
                    "authored_at": commit.authored_at.isoformat(),
                    "files": list(commit.files[:MAX_FILES]),
                },
                # The commit message is author-controlled free text. Tier D.
                content=commit.message[:MAX_SNIPPET_CHARS],
                untrusted=True,
                provenance_uri=commit.url or f"github://{commit.repo}/commit/{commit.sha}",
                observed_at=commit.authored_at,
            )
            recorded.append(item.id)

        for symbol in localization.symbols[:max_symbols]:
            item = await evidence_store.record(
                incident_id=incident_id,
                source=f"vcs:{symbol.repo}",
                source_type=SourceType.VCS,
                evidence_type=EvidenceType.CODE_SNIPPET,
                summary=(
                    f"{symbol.path}:L{symbol.start_line}-L{symbol.end_line}"
                    f"{' (' + symbol.symbol + ')' if symbol.symbol else ''} - {symbol.reason}"
                ),
                structured_value={
                    "repo": symbol.repo,
                    "ref": symbol.ref,
                    "path": symbol.path,
                    "symbol": symbol.symbol,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "matched_terms": list(symbol.matched_terms),
                    "commit_shas": list(symbol.commit_shas),
                    "evidence_trail": list(symbol.evidence_trail),
                },
                content=symbol.snippet,
                provenance_uri=symbol.provenance_uri,
            )
            recorded.append(item.id)

        if localization.degraded:
            # The gap is what stops a partial localisation from being read as a
            # complete one when the diagnosis is assembled.
            gap = await evidence_store.record_unavailable(
                incident_id=incident_id,
                source="code_localization",
                source_type=SourceType.VCS,
                reason=localization.degraded_reason or "code localisation degraded",
            )
            recorded.append(gap.id)

        log.info(
            "code localisation recorded",
            incident_id=incident_id,
            commits=len(localization.commits),
            symbols=len(localization.symbols),
            degraded=localization.degraded,
        )
        return recorded


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def symptom_terms(symptom: str, *, limit: int = MAX_SYMPTOM_TERMS) -> tuple[str, ...]:
    """Extract discriminating identifiers from symptom text.

    Deliberately biased towards code-shaped tokens (``dotted.names``,
    ``snake_case``, ``CamelCase``) because those are what appear in both a stack
    trace and a source file. Order is preserved so the query is deterministic.
    """
    seen: dict[str, None] = {}
    for match in _TERM_RE.finditer(symptom):
        token = match.group(0)
        if token.lower() in _STOPWORDS:
            continue
        seen.setdefault(token, None)
        if len(seen) >= limit:
            break
    return tuple(seen)


def localization_summary(localization: CodeLocalization) -> dict[str, Any]:
    """Compact, JSON-safe view for the API and the incident timeline."""
    return {
        "incident_id": localization.incident_id,
        "services": list(localization.service_ids),
        "repos": list(localization.repos),
        "terms": list(localization.symptom_terms),
        "counts": {
            "commits": len(localization.commits),
            "files": len(localization.files),
            "symbols": len(localization.symbols),
            "tests": len(localization.tests),
        },
        "stages": [
            {
                "name": s.name,
                "received": s.received,
                "emitted": s.emitted,
                "cap": s.cap,
                "truncated": s.truncated,
                "note": s.note,
            }
            for s in localization.stages
        ],
        "degraded": localization.degraded,
        "degraded_reason": localization.degraded_reason,
    }


__all__ = [
    "MAX_COMMITS",
    "MAX_FILES",
    "MAX_REPOS",
    "MAX_SYMBOLS",
    "MAX_TESTS",
    "CodeLocalization",
    "CodeRetriever",
    "CommitRecord",
    "CommitSource",
    "FileCandidate",
    "StageTrace",
    "SymbolCandidate",
    "TestCandidate",
    "localization_summary",
    "symptom_terms",
]
