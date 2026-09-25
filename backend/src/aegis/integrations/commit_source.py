"""Adapter from the GitHub client to the commit interface code retrieval needs.

``retrieval.code`` declares a ``CommitSource`` protocol rather than importing
the GitHub client, so a VCS outage degrades code localisation instead of
breaking the import graph. This module is the one place the two meet.

The join is not free: ``recent_commits`` returns metadata without a file list,
and the file list is what the next narrowing stage filters on. Fetching the
detail for every commit would be N+1 calls against a rate-limited API, so the
adapter fetches details only for the newest ``detail_budget`` commits and leaves
the rest with an empty file tuple. A commit with no file list still contributes
its message and its timing to ranking - it simply cannot be narrowed by path,
which is the correct degradation rather than a silent omission.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aegis.core.errors import SourceUnavailable
from aegis.core.logging import get_logger
from aegis.integrations.github import GitHubClient
from aegis.retrieval.code import CommitRecord

log = get_logger(__name__)

# Enough to cover the commits most likely to have caused an incident in the
# window, without turning one localisation into fifty API calls.
DEFAULT_DETAIL_BUDGET = 10


def split_repo(repo: str) -> tuple[str, str]:
    """Split ``owner/name``, refusing anything else.

    Guessing an owner would silently query the wrong repository and produce
    confident evidence about code that has nothing to do with the incident.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"repository {repo!r} must be in 'owner/name' form")
    return owner, name


class GitHubCommitSource:
    """Satisfies ``retrieval.code.CommitSource`` using the GitHub client."""

    __slots__ = ("_client", "_detail_budget")

    def __init__(
        self, client: GitHubClient, *, detail_budget: int = DEFAULT_DETAIL_BUDGET
    ) -> None:
        self._client = client
        self._detail_budget = max(0, detail_budget)

    async def commits_in_window(
        self, repo: str, *, since: datetime, until: datetime, limit: int
    ) -> Sequence[CommitRecord]:
        """Commits in the window, newest first, with bounded file detail.

        Propagates ``SourceUnavailable`` unchanged. The caller records the
        evidence gap, because "GitHub was unreachable" and "no commits in the
        window" support opposite conclusions about whether change caused this.
        """
        owner, name = split_repo(repo)
        summaries = await self._client.recent_commits(
            owner, name, since=since, until=until, limit=limit
        )

        records: list[CommitRecord] = []
        for index, summary in enumerate(summaries):
            files: tuple[str, ...] = ()
            if index < self._detail_budget:
                try:
                    detail = await self._client.get_commit(owner, name, summary.sha)
                    files = tuple(f.filename for f in detail.files)
                except SourceUnavailable as exc:
                    # One unreadable commit must not lose the other nineteen.
                    log.warning(
                        "commit detail unavailable",
                        repo=repo,
                        sha=summary.sha[:8],
                        reason=str(exc),
                    )
            records.append(
                CommitRecord(
                    repo=repo,
                    sha=summary.sha,
                    authored_at=summary.authored_at or until,
                    # The envelope is unwrapped here only to cross the interface;
                    # the retriever writes it back into untrusted evidence.
                    message=summary.message.text,
                    author=summary.author,
                    files=files,
                    url=summary.url,
                )
            )
        return records


__all__ = ["DEFAULT_DETAIL_BUDGET", "GitHubCommitSource", "split_repo"]
