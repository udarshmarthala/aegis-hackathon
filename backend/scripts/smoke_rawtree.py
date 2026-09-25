# ruff: noqa: T201 - a diagnostic script reports on stdout
"""Bounded live check of the RawTree integration. Never prints key values.

    backend/.venv/Scripts/python.exe backend/scripts/smoke_rawtree.py

1. inserts two rows into a scratch table ``aegis_smoke`` with the write key;
2. selects them back with the read key;
3. confirms each key is refused for the other's operation;
4. runs every named query once against the real server;
5. lists the MCP tools with the read key and shows what the allowlist keeps.

Writes only to ``aegis_smoke`` plus one ``metrics`` batch tagged
``run_id=smoke``; nothing else is touched.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))
os.chdir(BACKEND)

import httpx  # noqa: E402

from aegis.core.config import Settings  # noqa: E402
from aegis.domain.horizon import (  # noqa: E402
    HorizonEvent,
    HorizonEventType,
    HorizonPhase,
    Source,
)
from aegis.integrations.rawtree import (  # noqa: E402
    TABLE_METRICS,
    RawTreeClient,
    rawtree_ts,
    render_named_query,
)
from aegis.integrations.rawtree_mcp import ALLOWLIST, RawTreeAgentTools  # noqa: E402


def _scrub(text: str, *keys: str) -> str:
    for key in keys:
        if key:
            text = text.replace(key, "[KEY]")
    return text


async def main() -> int:
    s = Settings()
    wkey = s.rawtree_write_key.get_secret_value()
    rkey = s.rawtree_read_key.get_secret_value()
    print(
        f"write key set: {bool(wkey)}  read key set: {bool(rkey)}  "
        f"database: {s.rawtree_database or '(default)'}  api: {s.rawtree_api_url}"
    )
    if not (wkey and rkey):
        print("keys missing; nothing to verify")
        return 2

    base = s.rawtree_api_url.rstrip("/")
    params = {"database": s.rawtree_database} if s.rawtree_database else {}
    now = datetime.now(UTC)
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        rows = [
            {"ts": rawtree_ts(now), "probe": "smoke", "value": 1.5},
            {"ts": rawtree_ts(now), "probe": "smoke", "value": 2.5},
        ]
        r = await c.post(
            "/v1/tables/aegis_smoke",
            params=params,
            json=rows,
            headers={"Authorization": f"Bearer {wkey}"},
        )
        print("insert (write key):", r.status_code, _scrub(r.text[:200], wkey, rkey))
        r = await c.post(
            "/v1/query",
            params=params,
            json={"sql": "SELECT count() AS n, currentDatabase() AS db FROM aegis_smoke"},
            headers={"Authorization": f"Bearer {rkey}"},
        )
        print(
            "select (read key):", r.status_code, _scrub(r.text[:300], wkey, rkey).replace("\n", " ")
        )
        r = await c.post(
            "/v1/query", json={"sql": "SELECT 1"}, headers={"Authorization": f"Bearer {wkey}"}
        )
        print("query with write key (expect 403):", r.status_code)
        r = await c.post(
            "/v1/tables/aegis_smoke", json=rows[:1], headers={"Authorization": f"Bearer {rkey}"}
        )
        print("insert with read key (expect 403):", r.status_code)

    client = RawTreeClient(s)
    await client.start()
    client.enqueue_metrics(
        [
            {
                "ts": now,
                "run_id": "smoke",
                "service": "smoke-svc",
                "metric": "pool_utilisation",
                "value": 0.1,
            }
        ]
    )
    # One tagged event so the agent_events queries have a table to run
    # against; action_type is empty so it never counts toward success rates.
    client.enqueue_event(
        HorizonEvent(
            ts=now,
            run_id="smoke",
            incident_id="inc_smoke",
            step=1,
            phase=HorizonPhase.INVESTIGATING,
            event_type=HorizonEventType.STEP_COMPLETED,
            source=Source.SYSTEM,
            context_tokens=1200,
            naive_tokens=3400,
            message="smoke",
        )
    )
    await client.flush()
    print(
        "writer stats:",
        {k: v for k, v in client.stats().items() if k in ("inserted", "deferred", "last_error")},
    )
    for name, qp in (
        ("anomaly_detect", {}),
        ("action_success_rate", {}),
        ("context_tokens", {"run_id": "smoke"}),
        ("incident_timeline", {"incident_id": "inc_smoke"}),
    ):
        res = await client.named_query(name, qp)
        print(
            f"named {name}: source={res.source.value} rows={len(res.rows)} {res.rows[:2]} "
            f"ms={res.duration_ms} error={_scrub(str(res.error), wkey, rkey)[:200]}"
        )
    # The anomaly SQL's positive path, pointed at a scratch table so a live
    # heartbeat never sees synthetic data: a flat 0.30 baseline, then 0.90.
    scratch = "aegis_smoke_anomaly"
    # A fresh service name per run, so earlier runs' rows never sit in this
    # run's baseline window.
    svc = f"smoke-{int(now.timestamp())}"
    ramp = [
        {
            "ts": rawtree_ts(now - timedelta(seconds=90 + 30 * i)),
            "run_id": "smoke",
            "service": svc,
            "metric": "pool_utilisation",
            "value": 0.30 + 0.005 * (i % 3),
        }
        for i in range(12)
    ]
    ramp += [
        {
            "ts": rawtree_ts(now - timedelta(seconds=5 * i)),
            "run_id": "smoke",
            "service": svc,
            "metric": "pool_utilisation",
            "value": 0.90,
        }
        for i in range(3)
    ]
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        r = await c.post(
            f"/v1/tables/{scratch}",
            params=params,
            json=ramp,
            headers={"Authorization": f"Bearer {wkey}"},
        )
        print("anomaly scratch insert:", r.status_code)
    sql = render_named_query("anomaly_detect", {"service": svc}).replace(
        f"FROM {TABLE_METRICS} ", f"FROM {scratch} "
    )
    print("anomaly_detect positive path:", await client.query_sql(sql))
    await client.aclose()

    tools = RawTreeAgentTools(s)
    try:
        from aegis.mcp.remote import RemoteMCPClient

        # A wider allowlist for listing only - nothing here is ever called -
        # to show the dangerous tools exist server-side and are filtered.
        probe = frozenset(
            {"delete-table", "delete-database", "create-api-key", "insert-json", *ALLOWLIST}
        )
        raw = RemoteMCPClient(
            s.rawtree_mcp_url,
            rkey,
            allowlist=probe,
            timeout_s=s.rawtree_timeout_s,
            name="rawtree-smoke",
        )
        try:
            advertised = [t.name for t in await raw.list_tools()]
            print("server advertises (of probe set):", sorted(advertised))
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            print("mcp raw list failed:", _scrub(str(exc), wkey, rkey)[:200])
        specs = await tools.refresh()
        print("mcp tools exposed to the brain:", [t.name for t in specs])
        print("allowlist:", sorted(ALLOWLIST))
        res = await tools.call(
            "rawtree__run-query", {"sql": "SELECT count() AS n FROM aegis_metrics"}
        )
        print("mcp run-query:", res.sql, "rows:", res.rows[:3], "error:", res.error)
        res = await tools.call("rawtree__list-tables", {})
        print("mcp list-tables rows:", len(res.rows), "error:", res.error)
    except ImportError as exc:
        print("mcp remote client not available yet:", exc)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print("mcp check failed:", type(exc).__name__, _scrub(str(exc), wkey, rkey)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
