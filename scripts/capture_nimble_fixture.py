"""Re-capture ``backend/tests/fixtures/nimble_known_issue.json`` through Nimble.

The fixture is what ``NimbleKnownIssues`` returns when the live search cannot
run, so it must be a real result for a real issue, not something written by
hand to fit the story. This script runs the same search the agent runs (same
query, same ranking, the same two allowlisted tools) with a generous deadline,
and records which issue Nimble ranked where.

Excerpts are the one part that is not copied from the page: a committed file
holds a short, faithful summary per issue (``SUMMARIES``) rather than a slab of
someone else's text. An issue Nimble returns that has no reviewed summary is
recorded by title and URL only, and the script says so.

    backend/.venv/Scripts/python.exe scripts/capture_nimble_fixture.py

Requires ``NIMBLE_API_KEY`` in ``.env``. Never prints it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from aegis.core.config import Settings
from aegis.integrations.nimble import (
    DEFAULT_FIXTURE_PATH,
    MAX_EXCERPT_CHARS,
    NIMBLE_ALLOWLIST,
    SEARCH_RESULTS,
    SEARCH_TOOL,
    build_query,
    clean_text,
    parse_search,
    rank_results,
)
from aegis.mcp.remote import RemoteMCPClient

COMPONENT = "httpcore"
VERSION = "1.0.9"
CAPTURE_TIMEOUT_S = 45.0

# Reviewed summaries, keyed by URL: own words plus at most one short quote.
SUMMARIES: dict[str, str] = {
    "https://github.com/encode/httpx/issues/3782": (
        "Reported against httpx 0.28.1 with httpcore 1.0.9: when a task consuming an "
        "AsyncClient.stream() response is cancelled twice in quick succession, the httpcore "
        "connection pool keeps that connection counted as active after the task has ended. "
        "Each repetition strands another slot, so the pool drains gradually until "
        "max_connections is reached and new requests wait on the pool indefinitely. "
        "Reporter: connections \"appear to remain 'active' in the underlying pool after the "
        "task finishes\". The issue links a related httpcore pull request (#1113)."
    ),
}


async def capture() -> dict[str, object] | int:
    settings = Settings()
    key = settings.nimble_api_key.get_secret_value()
    if not key:
        print("NIMBLE_API_KEY is not set; nothing captured", file=sys.stderr)  # noqa: T201
        return 2
    client = RemoteMCPClient(
        settings.nimble_mcp_url, key, allowlist=NIMBLE_ALLOWLIST,
        timeout_s=CAPTURE_TIMEOUT_S, name="nimble",
    )
    query = build_query(COMPONENT, VERSION)
    async with client.session() as s:
        advertised = sorted(t.name for t in await s.list_tools())
        found = await s.call_tool(
            SEARCH_TOOL,
            {"query": query, "max_results": SEARCH_RESULTS, "output_format": "plain_text"},
        )
    ranked = rank_results(parse_search(found), COMPONENT, VERSION)
    issues = []
    for position, hit in enumerate(ranked, start=1):
        summary = SUMMARIES.get(hit["url"].rstrip("/"))
        if summary is None:
            print(f"no reviewed summary for {hit['url']}; skipped", file=sys.stderr)  # noqa: T201
            continue
        issues.append({
            "title": clean_text(hit["title"], 200),
            "url": hit["url"],
            "excerpt": clean_text(summary, MAX_EXCERPT_CHARS),
            "nimble_rank": position,
        })
    if not issues:
        print("Nimble returned none of the reviewed issues; fixture unchanged",  # noqa: T201
              file=sys.stderr)
        return 1
    return {
        "component": COMPONENT,
        "version": VERSION,
        "query": query,
        "captured_via": "nimble",
        "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "tools_allowlisted": advertised,
        "issues": issues[:2],
    }


def main() -> int:
    outcome = asyncio.run(capture())
    if isinstance(outcome, int):
        return outcome
    path = Path(DEFAULT_FIXTURE_PATH)
    path.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
