"""The agent's RawTree MCP connection: allowlist, read key, bounded SELECT only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from aegis.agents.horizon.ports import ToolSpec
from aegis.core.config import Settings
from aegis.core.errors import ValidationError
from aegis.domain.horizon import Source
from aegis.integrations.rawtree_mcp import ALLOWLIST, RawTreeAgentTools, validate_select

READ = "rt_read_TESTKEY_0002"
WRITE = "rt_write_TESTKEY_0001"

_SERVER_TOOLS = [
    "run-query",
    "list-tables",
    "describe-table",
    "delete-table",
    "delete-database",
    "create-api-key",
    "insert-json",
    "create-table",
    "update-table",
    "list-api-keys",
]


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "rawtree_read_key": READ,
        "rawtree_write_key": WRITE,
        "rawtree_database": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@dataclass
class Result:
    text: str
    structured: dict[str, Any] | None
    is_error: bool = False


class FakeRemote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[ToolSpec]:
        # A remote client that forgot to filter: the toolset must still do it.
        return [
            ToolSpec(name=n, description=n, input_schema={"type": "object"}) for n in _SERVER_TOOLS
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Result:
        self.calls.append((name, arguments))
        return Result(text="", structured={"data": [{"n": 1}], "rows": 1})


def _tools(**overrides: Any) -> tuple[RawTreeAgentTools, FakeRemote, list[Settings]]:
    remote = FakeRemote()
    seen: list[Settings] = []

    def factory(settings: Settings) -> FakeRemote:
        seen.append(settings)
        return remote

    return RawTreeAgentTools(_settings(**overrides), client_factory=factory), remote, seen


async def test_only_allowlisted_tools_reach_the_brain_prefixed() -> None:
    tools, _remote, _ = _tools()
    specs = await tools.refresh()
    names = {s.name for s in specs}
    assert names == {"rawtree__run-query", "rawtree__list-tables", "rawtree__describe-table"}
    for dangerous in ("delete-table", "delete-database", "create-api-key", "insert-json"):
        assert not any(dangerous in n for n in names)


@pytest.mark.parametrize(
    "name",
    [
        "rawtree__delete-table",
        "rawtree__delete-database",
        "rawtree__create-api-key",
        "rawtree__insert-json",
        "delete-table",
        "run-query",
        "rawtree__",
    ],
)
async def test_calling_anything_off_the_allowlist_raises_without_a_remote_call(name: str) -> None:
    tools, remote, _ = _tools()
    await tools.refresh()
    with pytest.raises(ValidationError):
        await tools.call(name, {"table": "aegis_metrics"})
    assert remote.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE aegis_metrics",
        "INSERT INTO aegis_metrics VALUES (1)",
        "SELECT 1; DROP TABLE aegis_metrics",
        "SELECT * FROM aegis_metrics; SELECT 1",
        "ALTER TABLE aegis_metrics DELETE WHERE 1",
        "TRUNCATE TABLE aegis_metrics",
        "SYSTEM FLUSH LOGS",
        "KILL QUERY WHERE 1",
        "GRANT ALL ON *.* TO x",
        "CREATE TABLE aegis_x (a Int8) ENGINE = Memory",
        "WITH x AS (SELECT 1) INSERT INTO aegis_metrics SELECT * FROM x",
        "SELECT * FROM url('http://evil.example/x', JSONEachRow)",
        "SELECT * FROM aegis_metrics -- sneaky",
        "SELECT * FROM aegis_metrics /* sneaky */",
        "SELECT * FROM system.tables",
        "SELECT * FROM agent_events",  # another producer's table in the shared database
        "SELECT * FROM aegis_metrics LIMIT 100000",
        "EXPLAIN SELECT 1",
        "",
    ],
)
async def test_non_select_or_unbounded_sql_is_rejected(sql: str) -> None:
    tools, remote, _ = _tools()
    with pytest.raises(ValidationError):
        await tools.call("rawtree__run-query", {"sql": sql})
    assert remote.calls == []


def test_select_gets_a_limit_and_literals_do_not_trip_keywords() -> None:
    assert validate_select("SELECT count() FROM aegis_metrics") == (
        "SELECT count() FROM aegis_metrics LIMIT 200"
    )
    sql = "SELECT * FROM aegis_agent_events WHERE message = 'DROP TABLE; --' LIMIT 20"
    assert validate_select(sql) == sql
    cte = "WITH recent AS (SELECT * FROM aegis_metrics) SELECT count() FROM recent"
    assert validate_select(cte).endswith("LIMIT 200")


async def test_run_query_sends_only_the_validated_sql() -> None:
    tools, remote, _ = _tools(rawtree_database="aegis")
    result = await tools.call(
        "rawtree__run-query",
        {"sql": "SELECT count() AS n FROM aegis_metrics", "organization": "other-org"},
    )
    assert remote.calls == [
        (
            "run-query",
            {"sql": "SELECT count() AS n FROM aegis_metrics LIMIT 200", "database": "aegis"},
        )
    ]
    assert result.name == "agent" and result.source is Source.RAWTREE
    assert result.rows == [{"n": 1}] and result.error is None


async def test_unavailable_without_read_key_even_if_write_key_is_set() -> None:
    tools, remote, seen = _tools(rawtree_read_key="")
    assert not tools.available
    assert tools.tool_specs() == []
    with pytest.raises(ValidationError):
        await tools.call("rawtree__list-tables", {})
    assert seen == [] and remote.calls == []


async def test_default_factory_connects_with_read_key_and_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Capture(FakeRemote):
        def __init__(self, url: str, bearer: str, **kw: Any) -> None:
            super().__init__()
            captured.update(url=url, bearer=bearer, **kw)

    monkeypatch.setattr("aegis.mcp.remote.RemoteMCPClient", Capture)
    tools = RawTreeAgentTools(_settings())
    await tools.refresh()
    assert captured["bearer"] == READ and captured["bearer"] != WRITE
    assert captured["allowlist"] == ALLOWLIST
