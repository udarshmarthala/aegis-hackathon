"""GitHub integration - change and deployment evidence, plus one guarded write.

Aegis reads broadly and writes narrowly (CLAUDE.md section 3.4). That rule is
enforced structurally here rather than by convention:

* ``_GitHubReads`` holds every method that only observes. Its names are listed in
  ``GitHubClient.READ_METHODS`` and the MCP tool layer may expose all of them.
* ``_GitHubWrites`` holds every method that changes state on GitHub. Its names
  are listed in ``GitHubClient.WRITE_METHODS``, the class refuses to run any of
  them unless writes were explicitly enabled at construction, and the tool layer
  must gate them behind the policy chain.

The two sets are disjoint, and a test asserts it, so a write cannot be added to
the read surface by accident.

Commit messages, PR bodies and deployment descriptions are free text authored by
whoever pushed the commit. They leave this module wrapped in ``UntrustedText``.

A missing token is not an error condition to paper over: reads raise
``SourceUnavailable`` (recorded as an evidence gap) and writes raise
``AuthorizationError``. Neither ever returns a fabricated response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final
from urllib.parse import quote

import httpx

from aegis.core.config import Settings
from aegis.core.errors import (
    AegisError,
    AuthorizationError,
    ExternalServiceError,
    NotFoundError,
    SourceUnavailable,
    ValidationError,
)
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call
from aegis.domain.models import UntrustedText

log = get_logger(__name__)

# A diff is evidence, not a payload to stream whole. Patches are truncated per
# file and the file list is capped, so a merge of ten thousand files cannot
# blow up the agent's context or the evidence table.
MAX_PATCH_CHARS: Final = 20_000
MAX_FILES_PER_COMMIT: Final = 100
MAX_BLOB_BYTES: Final = 256 * 1024
DEFAULT_BLOB_BYTES: Final = 64 * 1024
MAX_PAGE_SIZE: Final = 100

# GitHub starts throttling well before zero. Warning here gives an operator a
# chance to react before an investigation loses its change evidence.
RATE_LIMIT_WARN_BELOW: Final = 100

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class GitHubRateLimited(ExternalServiceError):
    """GitHub refused the call because the rate limit is exhausted.

    Retryable in the sense that the caller may try again *later* - ``reset_at``
    says when. It is deliberately raised outside the retry loop so that hitting
    the limit never turns into hammering the limit.
    """

    code = "GITHUB_RATE_LIMITED"
    http_status = 429
    retryable = True


class _RateLimitSignal(AegisError):
    """Internal marker raised inside the guarded call.

    ``retryable = False`` so ``retry_async`` re-raises immediately; the outer
    layer converts it into the public ``GitHubRateLimited``. Without this split
    a rate-limited read would retry straight back into the same wall.
    """

    code = "GITHUB_RATE_LIMITED_INTERNAL"
    retryable = False


# --------------------------------------------------------------------------- #
# normalised shapes                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CommitSummary:
    sha: str
    message: UntrustedText
    author: str
    authored_at: datetime | None
    url: str


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One file in a diff. ``patch`` is bounded and may be truncated."""

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    patch_truncated: bool


@dataclass(frozen=True, slots=True)
class CommitDetail:
    summary: CommitSummary
    files: tuple[ChangedFile, ...]
    additions: int
    deletions: int
    files_truncated: bool


@dataclass(frozen=True, slots=True)
class FileContent:
    """A bounded slice of a blob. ``truncated`` is never inferred by a caller."""

    path: str
    ref: str
    text: str
    bytes_read: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class Comparison:
    base: str
    head: str
    status: str
    ahead_by: int
    behind_by: int
    commits: tuple[CommitSummary, ...]
    files: tuple[ChangedFile, ...]


@dataclass(frozen=True, slots=True)
class CodeSearchHit:
    path: str
    repository: str
    url: str


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: int
    name: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    created_at: datetime | None
    updated_at: datetime | None
    url: str


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    id: int
    sha: str
    ref: str
    task: str
    environment: str
    description: UntrustedText
    created_at: datetime | None
    url: str


@dataclass(frozen=True, slots=True)
class PullRequestResult:
    """The audit record of a write. ``performed`` is the exact call made."""

    number: int
    url: str
    state: str
    draft: bool
    head: str
    base: str
    performed: str


# --------------------------------------------------------------------------- #
# input validation                                                             #
# --------------------------------------------------------------------------- #


def _repo_part(name: str, value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValidationError(
            f"invalid github {name}", context={name: value[:64]}
        )
    return value


def _ref(value: str) -> str:
    if not _REF_RE.match(value) or ".." in value:
        raise ValidationError("invalid git ref", context={"ref": value[:64]})
    return value


def _sha(value: str) -> str:
    if not _SHA_RE.match(value):
        raise ValidationError("invalid commit sha", context={"sha": value[:64]})
    return value


def _repo_path(value: str) -> str:
    """Validate a repository-relative path.

    Traversal is refused rather than normalised: a path that tries to climb out
    of the repo is a caller bug or an injection attempt, and silently rewriting
    it would hide both.
    """
    if not value or value.startswith("/") or ".." in value.split("/") or "\x00" in value:
        raise ValidationError("invalid repository path", context={"path": value[:128]})
    if len(value) > 512:
        raise ValidationError("repository path too long", context={"length": len(value)})
    return value


def sanitise_search_query(query: str) -> str:
    """Strip GitHub search qualifiers out of a free-text query.

    The query reaches here from an agent, which means from model output. A
    qualifier such as ``repo:`` would let it widen the search past the repository
    the caller scoped, so the colon and the quoting characters come out and the
    scope qualifier is appended by us afterwards.
    """
    cleaned = re.sub(r'[:"\n\r]+', " ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValidationError("search query is empty after sanitisation")
    return cleaned[:200]


def _as_dict(raw: object) -> dict[str, Any]:
    """Narrow an untyped JSON member to a mapping.

    External payloads are ``Any`` all the way down; funnelling every lookup
    through one narrowing helper keeps the parsing code readable and keeps a
    ``None`` from turning into an AttributeError three frames later.
    """
    return raw if isinstance(raw, dict) else {}


def _as_list(raw: object) -> list[Any]:
    """Narrow an untyped JSON member to a list, for the same reason as above."""
    return raw if isinstance(raw, list) else []


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _commit_summary(payload: dict[str, Any]) -> CommitSummary:
    commit = _as_dict(payload.get("commit"))
    author = _as_dict(commit.get("author"))
    sha = str(payload.get("sha", ""))
    return CommitSummary(
        sha=sha,
        # Tier D: a commit message is whatever the pusher typed.
        message=UntrustedText(
            text=str(commit.get("message", "")), origin="commit_message"
        ),
        author=str(author.get("name", "")),
        authored_at=_parse_ts(author.get("date")),
        url=str(payload.get("html_url", "")),
    )


def _changed_files(raw: Any) -> tuple[tuple[ChangedFile, ...], bool]:
    files: list[ChangedFile] = []
    if not isinstance(raw, list):
        return (), False
    truncated = len(raw) > MAX_FILES_PER_COMMIT
    for item in raw[:MAX_FILES_PER_COMMIT]:
        if not isinstance(item, dict):
            continue
        patch = item.get("patch")
        patch_text = str(patch) if isinstance(patch, str) else None
        patch_truncated = patch_text is not None and len(patch_text) > MAX_PATCH_CHARS
        files.append(
            ChangedFile(
                filename=str(item.get("filename", "")),
                status=str(item.get("status", "")),
                additions=int(item.get("additions", 0) or 0),
                deletions=int(item.get("deletions", 0) or 0),
                patch=patch_text[:MAX_PATCH_CHARS] if patch_text is not None else None,
                patch_truncated=patch_truncated,
            )
        )
    return tuple(files), truncated


# --------------------------------------------------------------------------- #
# transport                                                                    #
# --------------------------------------------------------------------------- #


class _GitHubBase:
    """Connection, auth and failure handling shared by both halves."""

    __slots__ = ("_settings", "_bulkhead", "_client", "_allow_writes")

    def __init__(self, settings: Settings, *, allow_writes: bool = False) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead("github", limit=6)
        self._client: httpx.AsyncClient | None = None
        # Writes are off unless a caller opts in, so importing the client is
        # never enough to gain the ability to change a repository.
        self._allow_writes = allow_writes

    @property
    def configured(self) -> bool:
        return bool(self._settings.github_token.get_secret_value())

    @property
    def writes_enabled(self) -> bool:
        return self._allow_writes and self.configured

    @property
    def default_owner(self) -> str:
        return self._settings.github_default_owner

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            token = self._settings.github_token.get_secret_value()
            self._client = httpx.AsyncClient(
                base_url=self._settings.github_api_url,
                timeout=self._settings.source_timeout_s,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": f"aegis/{self._settings.aegis_version}",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_read(self, operation: str) -> None:
        if not self.configured:
            raise SourceUnavailable(
                "github not configured: no github_token is set",
                context={"dependency": "github", "operation": operation},
            )

    def _require_write(self, operation: str) -> None:
        """Writes need a token *and* an explicit opt-in. Fail closed on both."""
        if not self.configured:
            raise AuthorizationError(
                "github not configured: no github_token is set",
                context={"dependency": "github", "operation": operation},
            )
        if not self._allow_writes:
            raise AuthorizationError(
                "github writes are not enabled on this client",
                context={"dependency": "github", "operation": operation},
            )

    def _note_rate_limit(self, resp: httpx.Response) -> None:
        """Record remaining quota. Never logs the token or the URL's query."""
        raw = resp.headers.get("X-RateLimit-Remaining")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        if remaining < RATE_LIMIT_WARN_BELOW:
            log.warning(
                "github rate limit low",
                remaining=remaining,
                reset_at=resp.headers.get("X-RateLimit-Reset", ""),
                path=resp.request.url.path,
            )

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.status_code != 403:
            return False
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        return "rate limit" in resp.text[:500].lower()

    def _raise_for_status(self, resp: httpx.Response, operation: str) -> None:
        """Map GitHub's status codes onto the typed hierarchy."""
        if resp.is_success:
            return
        ctx = {"operation": operation, "status": resp.status_code, "path": resp.request.url.path}
        if self._is_rate_limited(resp):
            raise _RateLimitSignal(
                "github rate limit exhausted",
                context={**ctx, "reset_at": resp.headers.get("X-RateLimit-Reset", "")},
            )
        if resp.status_code in (401, 403):
            raise AuthorizationError("github rejected the credential", context=ctx)
        if resp.status_code == 404:
            raise NotFoundError("github resource not found", context=ctx)
        if resp.status_code == 422:
            raise ValidationError("github rejected the request body", context=ctx)
        if resp.status_code >= 500:
            # Server-side and safe to retry for an idempotent call; the caller
            # decides how many attempts it is willing to spend.
            raise ExternalServiceError("github server error", context=ctx)
        raise ExternalServiceError(
            f"github returned {resp.status_code}", context=ctx, retryable=False
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 2,
    ) -> httpx.Response:
        """One guarded call. ``attempts`` must be 1 for anything non-idempotent."""

        async def _call() -> httpx.Response:
            client = await self._http()
            resp = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
            self._note_rate_limit(resp)
            self._raise_for_status(resp, operation)
            return resp

        try:
            return await guarded_call(
                _call,
                dependency="github",
                timeout_s=self._settings.source_timeout_s,
                attempts=attempts,
                bulkhead=self._bulkhead,
            )
        except _RateLimitSignal as exc:
            raise GitHubRateLimited(exc.message, context=exc.context) from exc
        except AegisError:
            # Already typed and meaningful - a 404 is not "github is down".
            raise
        except Exception as exc:
            raise SourceUnavailable(
                f"github unavailable: {type(exc).__name__}",
                context={"dependency": "github", "operation": operation},
            ) from exc

    async def _json(self, *args: Any, **kwargs: Any) -> Any:
        resp = await self._request(*args, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceUnavailable(
                "github returned a non-JSON body",
                context={"dependency": "github", "path": resp.request.url.path},
            ) from exc


# --------------------------------------------------------------------------- #
# read surface                                                                 #
# --------------------------------------------------------------------------- #


class _GitHubReads(_GitHubBase):
    """Observation only. Nothing here changes state on GitHub."""

    __slots__ = ()

    async def rate_limit(self) -> dict[str, int]:
        """Remaining API quota. Cheap, unauthenticated-safe, and not itself rated.

        Used by the integration-health surface: it proves the credential works
        without changing anything and without spending quota.
        """
        self._require_read("rate_limit")
        payload = await self._json("GET", "/rate_limit", operation="rate_limit", attempts=1)
        core = _as_dict(_as_dict(_as_dict(payload).get("resources")).get("core"))
        return {
            "limit": int(core.get("limit", 0) or 0),
            "remaining": int(core.get("remaining", 0) or 0),
            "reset": int(core.get("reset", 0) or 0),
        }

    async def recent_commits(
        self,
        owner: str,
        repo: str,
        *,
        since: datetime,
        until: datetime | None = None,
        path: str | None = None,
        limit: int = 30,
    ) -> list[CommitSummary]:
        """Commits in a window, optionally scoped to one path.

        An empty list means the window held no commit. It never means GitHub was
        unreachable - that raises.
        """
        self._require_read("recent_commits")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        params: dict[str, Any] = {
            "since": _iso(since),
            "per_page": _page_size(limit),
        }
        if until is not None:
            params["until"] = _iso(until)
        if path is not None:
            params["path"] = _repo_path(path)

        payload = await self._json(
            "GET",
            f"/repos/{owner}/{repo}/commits",
            operation="recent_commits",
            params=params,
        )
        rows = _as_list(payload)
        return [_commit_summary(row) for row in rows[:limit] if isinstance(row, dict)]

    async def get_commit(self, owner: str, repo: str, sha: str) -> CommitDetail:
        """One commit with its bounded diff."""
        self._require_read("get_commit")
        owner, repo, sha = _repo_part("owner", owner), _repo_part("repo", repo), _sha(sha)
        payload = await self._json(
            "GET", f"/repos/{owner}/{repo}/commits/{sha}", operation="get_commit"
        )
        if not isinstance(payload, dict):
            raise SourceUnavailable(
                "github returned an unexpected commit shape",
                context={"dependency": "github", "sha": sha},
            )
        stats = _as_dict(payload.get("stats"))
        files, truncated = _changed_files(payload.get("files"))
        return CommitDetail(
            summary=_commit_summary(payload),
            files=files,
            additions=int(stats.get("additions", 0) or 0),
            deletions=int(stats.get("deletions", 0) or 0),
            files_truncated=truncated,
        )

    async def file_at_ref(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str,
        *,
        max_bytes: int = DEFAULT_BLOB_BYTES,
    ) -> FileContent:
        """Read at most ``max_bytes`` of a file at a ref.

        The bound is enforced with a Range request rather than by truncating
        after download: a vendored lockfile or a checked-in binary would
        otherwise be pulled over the wire in full before being thrown away.
        """
        self._require_read("file_at_ref")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        path, ref = _repo_path(path), _ref(ref)
        if max_bytes < 1:
            raise ValidationError("max_bytes must be >= 1", context={"max_bytes": max_bytes})
        cap = min(max_bytes, MAX_BLOB_BYTES)

        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{quote(path)}",
            operation="file_at_ref",
            params={"ref": ref},
            headers={
                "Accept": "application/vnd.github.raw",
                "Range": f"bytes=0-{cap - 1}",
            },
        )
        body = resp.content[:cap]
        # 206 means the server honoured the range; a full 200 that lands exactly
        # on the cap is indistinguishable from a longer file, so it counts as
        # truncated too. Over-reporting truncation is the safe direction.
        truncated = resp.status_code == 206 or len(resp.content) >= cap
        return FileContent(
            path=path,
            ref=ref,
            text=body.decode("utf-8", errors="replace"),
            bytes_read=len(body),
            truncated=truncated,
        )

    async def compare(self, owner: str, repo: str, base: str, head: str) -> Comparison:
        """Diff two refs - the change-analysis workhorse."""
        self._require_read("compare")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        base, head = _ref(base), _ref(head)
        payload = await self._json(
            "GET",
            f"/repos/{owner}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
            operation="compare",
        )
        if not isinstance(payload, dict):
            raise SourceUnavailable(
                "github returned an unexpected compare shape",
                context={"dependency": "github", "base": base, "head": head},
            )
        commits = _as_list(payload.get("commits"))
        files, _ = _changed_files(payload.get("files"))
        return Comparison(
            base=base,
            head=head,
            status=str(payload.get("status", "")),
            ahead_by=int(payload.get("ahead_by", 0) or 0),
            behind_by=int(payload.get("behind_by", 0) or 0),
            commits=tuple(
                _commit_summary(c) for c in commits[:MAX_PAGE_SIZE] if isinstance(c, dict)
            ),
            files=files,
        )

    async def search_code(
        self, owner: str, repo: str, query: str, limit: int = 20
    ) -> list[CodeSearchHit]:
        """Code search, hard-scoped to one repository.

        The scope qualifier is appended after the caller's text is stripped of
        qualifier syntax, so the search cannot be widened by its input.
        """
        self._require_read("search_code")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        payload = await self._json(
            "GET",
            "/search/code",
            operation="search_code",
            params={
                "q": f"{sanitise_search_query(query)} repo:{owner}/{repo}",
                "per_page": _page_size(limit),
            },
        )
        items = _as_list(_as_dict(payload).get("items"))
        out: list[CodeSearchHit] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            repository = item.get("repository")
            out.append(
                CodeSearchHit(
                    path=str(item.get("path", "")),
                    repository=str(
                        repository.get("full_name", "") if isinstance(repository, dict) else ""
                    ),
                    url=str(item.get("html_url", "")),
                )
            )
        return out

    async def list_workflow_runs(
        self, owner: str, repo: str, limit: int = 20
    ) -> list[WorkflowRun]:
        """Recent CI runs - the "did a build change something" evidence."""
        self._require_read("list_workflow_runs")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        payload = await self._json(
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            operation="list_workflow_runs",
            params={"per_page": _page_size(limit)},
        )
        runs = _as_list(_as_dict(payload).get("workflow_runs"))
        out: list[WorkflowRun] = []
        for row in runs[:limit]:
            if not isinstance(row, dict):
                continue
            conclusion = row.get("conclusion")
            out.append(
                WorkflowRun(
                    id=int(row.get("id", 0) or 0),
                    name=str(row.get("name", "")),
                    head_sha=str(row.get("head_sha", "")),
                    event=str(row.get("event", "")),
                    status=str(row.get("status", "")),
                    conclusion=str(conclusion) if conclusion else None,
                    created_at=_parse_ts(row.get("created_at")),
                    updated_at=_parse_ts(row.get("updated_at")),
                    url=str(row.get("html_url", "")),
                )
            )
        return out

    async def get_deployments(
        self, owner: str, repo: str, environment: str, limit: int = 20
    ) -> list[DeploymentRecord]:
        """Deployment records for one environment, newest first."""
        self._require_read("get_deployments")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        env = _repo_part("environment", environment)
        payload = await self._json(
            "GET",
            f"/repos/{owner}/{repo}/deployments",
            operation="get_deployments",
            params={"environment": env, "per_page": _page_size(limit)},
        )
        rows = _as_list(payload)
        out: list[DeploymentRecord] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            out.append(
                DeploymentRecord(
                    id=int(row.get("id", 0) or 0),
                    sha=str(row.get("sha", "")),
                    ref=str(row.get("ref", "")),
                    task=str(row.get("task", "")),
                    environment=str(row.get("environment", env)),
                    # Tier D: a deploy description is free text from whoever
                    # triggered the deployment.
                    description=UntrustedText(
                        text=str(row.get("description") or ""), origin="deployment_description"
                    ),
                    created_at=_parse_ts(row.get("created_at")),
                    url=str(row.get("url", "")),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# write surface                                                                #
# --------------------------------------------------------------------------- #


class _GitHubWrites(_GitHubBase):
    """State-changing calls. Every method here is gated and never auto-retried."""

    __slots__ = ()

    async def open_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> PullRequestResult:
        """Open a pull request. WRITE - policy-gated, single attempt, draft by default.

        Draft is the default because a proposed fix is a proposal: opening it
        ready-for-review would let an automated patch enter a merge queue without
        a human having looked at it.

        Opening a PR is not idempotent - a second call creates a second PR - so
        ``attempts=1`` and the caller owns any decision to try again.
        """
        self._require_write("open_pull_request")
        owner, repo = _repo_part("owner", owner), _repo_part("repo", repo)
        head, base = _ref(head), _ref(base)
        if not title.strip():
            raise ValidationError("pull request title must not be empty")

        payload = await self._json(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            operation="open_pull_request",
            json_body={
                "title": title[:250],
                "head": head,
                "base": base,
                "body": body[:60_000],
                "draft": draft,
            },
            attempts=1,
        )
        if not isinstance(payload, dict):
            raise ExternalServiceError(
                "github returned an unexpected pull request shape",
                context={"dependency": "github"},
                retryable=False,
            )
        result = PullRequestResult(
            number=int(payload.get("number", 0) or 0),
            url=str(payload.get("html_url", "")),
            state=str(payload.get("state", "")),
            draft=bool(payload.get("draft", draft)),
            head=head,
            base=base,
            performed=f"POST /repos/{owner}/{repo}/pulls head={head} base={base} draft={draft}",
        )
        log.info(
            "github pull request opened",
            repo=f"{owner}/{repo}",
            number=result.number,
            draft=result.draft,
        )
        return result


class GitHubClient(_GitHubReads, _GitHubWrites):
    """The GitHub tool boundary.

    ``READ_METHODS`` may be exposed to agents directly. ``WRITE_METHODS`` must be
    routed through the policy chain, and are inert unless the client was built
    with ``allow_writes=True``.
    """

    __slots__ = ()

    READ_METHODS: ClassVar[frozenset[str]] = frozenset(
        {
            "rate_limit",
            "recent_commits",
            "get_commit",
            "file_at_ref",
            "compare",
            "search_code",
            "list_workflow_runs",
            "get_deployments",
        }
    )
    WRITE_METHODS: ClassVar[frozenset[str]] = frozenset({"open_pull_request"})


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ValidationError("limit must be >= 1", context={"limit": limit})
    return min(limit, MAX_PAGE_SIZE)


__all__ = [
    "DEFAULT_BLOB_BYTES",
    "MAX_BLOB_BYTES",
    "MAX_FILES_PER_COMMIT",
    "MAX_PATCH_CHARS",
    "RATE_LIMIT_WARN_BELOW",
    "ChangedFile",
    "CodeSearchHit",
    "CommitDetail",
    "CommitSummary",
    "Comparison",
    "DeploymentRecord",
    "FileContent",
    "GitHubClient",
    "GitHubRateLimited",
    "PullRequestResult",
    "WorkflowRun",
    "sanitise_search_query",
]
