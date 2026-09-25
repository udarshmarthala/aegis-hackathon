"""External evidence: known regressions in a dependency, via Nimble's web search.

When a recent deploy changed a dependency, the question "is this a known bug in
that version?" is answered by the public web, not by our telemetry. Nimble's
remote MCP server supplies that answer with two tools, and only two:

``nimble_search``   one web search, results carrying page text
``nimble_extract``  one known URL, fetched

Everything else the server advertises - crawls, site maps, extraction-template
generation, web-search *agents* that create, run and delete jobs - is filtered
out by the ``RemoteMCPClient`` allowlist. They are slower, metered and in
several cases write to the account, and none of them is needed to read an issue
page.

The search runs under one deadline (``settings.nimble_timeout_s``). Any failure
- no key, a timeout, a transport fault, a tool error, zero usable results -
returns the committed fixture with ``source=fixture`` and a ``reason``. The
fixture is a real search result for a real issue (see ``captured_via`` in the
file), so the demo narrative is identical either way and the UI says which path
ran. ``search_known_issues`` never raises.

Page text is attacker-influenceable: anyone can write an issue. Excerpts are
bounded, stripped of control characters and handed onward as data
(``untrusted_excerpt``), never as instruction.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from aegis.agents.horizon.ports import KnownIssue, KnownIssueResult
from aegis.core.config import Settings
from aegis.core.errors import AegisError
from aegis.core.logging import get_logger
from aegis.core.resilience import with_timeout
from aegis.domain.horizon import Source
from aegis.domain.models import UntrustedText
from aegis.mcp.remote import RemoteCallResult, RemoteMCPClient, RemoteMCPSession

log = get_logger(__name__)

SEARCH_TOOL: Final = "nimble_search"
EXTRACT_TOOL: Final = "nimble_extract"
# Confirmed against the live server's tools/list; see the module docstring for
# what is deliberately left out.
NIMBLE_ALLOWLIST: Final = frozenset({SEARCH_TOOL, EXTRACT_TOOL})

MAX_EXCERPT_CHARS: Final = 600
MAX_TITLE_CHARS: Final = 200
MAX_ISSUES: Final = 2
SEARCH_RESULTS: Final = 5
# Extraction is only worth starting with this much of the deadline left; a
# half-finished extract is wasted spend and the search text is usually enough.
_EXTRACT_MIN_REMAINING_S: Final = 3.0
# A search result whose page text is shorter than this gets an extract.
_THIN_DESCRIPTION_CHARS: Final = 200

# In a source checkout the fixture sits beside the tests; in the image the
# package is installed into site-packages, so the image names the directory
# explicitly with AEGIS_FIXTURES_DIR.
DEFAULT_FIXTURE_PATH: Final = (
    Path(os.environ["AEGIS_FIXTURES_DIR"]) / "nimble_known_issue.json"
    if os.environ.get("AEGIS_FIXTURES_DIR")
    else Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "nimble_known_issue.json"
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# GitHub page chrome that search engines capture along with the issue body.
_CHROME_RE = re.compile(r"\b(Copy link|Skip to content)\b")
_SPACE_RE = re.compile(r"\s+")
_GITHUB_ISSUE_RE = re.compile(r"^/[^/]+/[^/]+/(issues|pull)/\d+/?$")
_GITHUB_DISCUSSION_RE = re.compile(r"^/[^/]+/[^/]+/discussions/\d+/?$")


def build_query(component: str, version: str) -> str:
    return f"{component} {version} connections remain active in pool after cancel github issue"


def clean_text(text: str, limit: int) -> str:
    """Collapse whitespace, drop control characters, bound the length."""
    text = _CHROME_RE.sub(" ", _CONTROL_RE.sub(" ", text))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def untrusted_excerpt(issue: KnownIssue) -> UntrustedText:
    """The excerpt as the envelope a prompt or compactor must receive it in."""
    return UntrustedText(text=f"{issue.title}\n{issue.url}\n{issue.excerpt}", origin="nimble")


class NimbleKnownIssues:
    """``KnownIssueSearch`` over Nimble's remote MCP server, fixture-backed."""

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], RemoteMCPClient] | None = None,
        fixture_path: Path | str | None = None,
    ) -> None:
        self._settings = settings
        self._timeout_s = settings.nimble_timeout_s
        key = settings.nimble_api_key.get_secret_value()
        self._configured = bool(key and settings.nimble_mcp_url)
        # Read once, here: a file read on the fallback path would block the
        # event loop at exactly the moment something else has already gone wrong.
        self._fixture_data, self._fixture_error = _load_fixture(
            Path(fixture_path) if fixture_path else DEFAULT_FIXTURE_PATH
        )
        if client_factory is not None:
            self._client: RemoteMCPClient = client_factory()
        else:
            self._client = RemoteMCPClient(
                settings.nimble_mcp_url,
                key,
                allowlist=NIMBLE_ALLOWLIST,
                timeout_s=self._timeout_s,
                name="nimble",
            )

    @property
    def configured(self) -> bool:
        return self._configured

    def status(self) -> dict[str, Any]:
        """For the war-room integrations badge. No secrets."""
        return {
            "configured": self._configured,
            "reason": "" if self._configured else "NIMBLE_API_KEY is not set",
        }

    async def search_known_issues(self, component: str, version: str) -> KnownIssueResult:
        query = build_query(component, version)
        started = time.monotonic()
        if not self._configured:
            return self._fixture(component, version, query, "NIMBLE_API_KEY is not set", started)
        try:
            issues = await self._live(component, version, query, started)
        except AegisError as exc:
            return self._fixture(component, version, query, f"nimble {exc.code}: {exc.message}",
                                 started)
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            return self._fixture(component, version, query,
                                 f"nimble failed: {type(exc).__name__}", started)
        if not issues:
            return self._fixture(component, version, query, "nimble returned no usable results",
                                 started)
        return KnownIssueResult(
            issues=issues, source=Source.NIMBLE, query=query,
            duration_ms=_elapsed_ms(started),
        )

    # -- live path -----------------------------------------------------------

    async def _live(
        self, component: str, version: str, query: str, started: float
    ) -> list[KnownIssue]:
        async def run() -> list[KnownIssue]:
            async with self._client.session() as s:
                tools = {t.name for t in await s.list_tools()}
                if SEARCH_TOOL not in tools:
                    return []
                found = await s.call_tool(
                    SEARCH_TOOL,
                    {"query": query, "max_results": SEARCH_RESULTS, "output_format": "plain_text"},
                )
                ranked = rank_results(parse_search(found), component, version)[:MAX_ISSUES]
                issues: list[KnownIssue] = []
                for hit in ranked:
                    body = hit["description"]
                    if (
                        len(body) < _THIN_DESCRIPTION_CHARS
                        and EXTRACT_TOOL in tools
                        and self._timeout_s - (time.monotonic() - started)
                        >= _EXTRACT_MIN_REMAINING_S
                    ):
                        body = await self._extract(s, hit["url"]) or body
                    excerpt = clean_text(body, MAX_EXCERPT_CHARS)
                    if not excerpt:
                        continue
                    issues.append(
                        KnownIssue(
                            title=clean_text(hit["title"], MAX_TITLE_CHARS),
                            url=hit["url"], excerpt=excerpt,
                            component=component, version=version,
                        )
                    )
                return issues

        # One deadline over handshake, search and extract together.
        return await with_timeout(run(), self._timeout_s, what="nimble.search_known_issues")

    async def _extract(self, s: RemoteMCPSession, url: str) -> str:
        res = await s.call_tool(EXTRACT_TOOL, {"url": url, "output_format": "markdown"})
        if res.is_error:
            return ""
        if res.structured and isinstance(res.structured.get("content"), str):
            return str(res.structured["content"])
        return res.text

    # -- fallback ------------------------------------------------------------

    def _fixture(
        self, component: str, version: str, query: str, reason: str, started: float
    ) -> KnownIssueResult:
        log.info("nimble fallback to fixture", reason=reason, component=component)
        issues: list[KnownIssue] = []
        data = self._fixture_data
        if data is None:
            reason = f"{reason}; fixture unreadable ({self._fixture_error})"
        else:
            try:
                for item in data.get("issues", [])[:MAX_ISSUES]:
                    issues.append(
                        KnownIssue(
                            title=clean_text(str(item["title"]), MAX_TITLE_CHARS),
                            url=str(item["url"]),
                            excerpt=clean_text(str(item["excerpt"]), MAX_EXCERPT_CHARS),
                            component=str(data.get("component", component)),
                            version=str(data.get("version", version)),
                        )
                    )
                reason = f"{reason}; fixture captured via {data.get('captured_via', 'unknown')}"
            except (KeyError, TypeError, AttributeError) as exc:
                issues = []
                reason = f"{reason}; fixture malformed ({type(exc).__name__})"
        return KnownIssueResult(
            issues=issues, source=Source.FIXTURE, query=query, reason=reason[:300],
            duration_ms=_elapsed_ms(started),
        )


# --------------------------------------------------------------------------- #
# result parsing and ranking (pure; unit-tested directly)                      #
# --------------------------------------------------------------------------- #


def parse_search(result: RemoteCallResult) -> list[dict[str, str]]:
    """Nimble's search answer -> ``[{title, url, description}]``.

    The payload is ``{"results": [{"title", "url", "description", "content"}]}``
    in ``structuredContent`` and, serialised, in the text part. Anything that is
    not an https URL is dropped: the URL is shown to an operator as a link.
    """
    if result.is_error:
        return []
    payload: Any = result.structured
    if payload is None:
        try:
            payload = json.loads(result.text)
        except ValueError:
            return []
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows[:SEARCH_RESULTS * 2]:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if urlsplit(url).scheme != "https":
            continue
        body = str(row.get("content") or "") or str(row.get("description") or "")
        out.append({"title": str(row.get("title") or url), "url": url, "description": body})
    return out


def rank_results(
    rows: list[dict[str, str]], component: str, version: str
) -> list[dict[str, str]]:
    """GitHub issues first, then discussions, then pages naming the component."""

    def score(row: dict[str, str]) -> int:
        parts = urlsplit(row["url"])
        hay = f"{row['title']} {row['description'][:2000]}".lower()
        s = 0
        if parts.hostname == "github.com":
            if _GITHUB_ISSUE_RE.match(parts.path):
                s += 100
            elif _GITHUB_DISCUSSION_RE.match(parts.path):
                s += 40
        if component.lower() in hay:
            s += 20
        if version and version in hay:
            s += 10
        if any(w in hay for w in ("leak", "pooltimeout", "pool timeout", "exhaust")):
            s += 15
        return s

    return sorted(rows, key=score, reverse=True)


def _load_fixture(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("nimble fixture unreadable", path=str(path), error=type(exc).__name__)
        return None, type(exc).__name__
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, ""


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "DEFAULT_FIXTURE_PATH",
    "EXTRACT_TOOL",
    "NIMBLE_ALLOWLIST",
    "SEARCH_TOOL",
    "NimbleKnownIssues",
    "build_query",
    "clean_text",
    "parse_search",
    "rank_results",
    "untrusted_excerpt",
]
