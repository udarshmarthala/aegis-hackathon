"""The agent's own connection to RawTree over MCP.

The brain may look at its history; it may not change it. Three layers make
that structural rather than a matter of the model behaving:

1. the connection uses the **read** key, which RawTree refuses for any write
   (verified: a read-only key gets 403 on insert);
2. an allowlist of exactly ``run-query``, ``list-tables`` and
   ``describe-table`` - everything else the server advertises
   (``delete-table``, ``delete-database``, ``create-api-key`` ...) is removed
   before the brain sees the list, and calling it raises;
3. ``run-query`` SQL must be a single bounded ``SELECT`` (or ``WITH ... SELECT``);
   a missing ``LIMIT`` gets ``LIMIT 200``.

Layer 3 duplicates what the key already enforces. It is defence in depth: a
mis-issued key with write scope must still not give the model a write path.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Final, Protocol

from aegis.agents.horizon.ports import QueryResult, ToolSpec
from aegis.core.config import Settings
from aegis.core.errors import AegisError, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.horizon import Source
from aegis.integrations.rawtree import TABLE_PREFIX

log = get_logger(__name__)

PREFIX: Final = "rawtree__"
ALLOWLIST: Final = frozenset({"run-query", "list-tables", "describe-table"})
DEFAULT_LIMIT: Final = 200
MAX_SQL_CHARS: Final = 4000
MAX_ROWS: Final = 200

_FORBIDDEN: Final = re.compile(
    r"\b(INSERT|ALTER|DROP|CREATE|TRUNCATE|SYSTEM|KILL|GRANT|REVOKE|DELETE|UPDATE|"
    r"RENAME|ATTACH|DETACH|OPTIMIZE|EXCHANGE|SET|USE|INTO\s+OUTFILE)\b",
    re.IGNORECASE,
)
# Table functions that reach outside the database (url(), s3(), file() ...).
_FORBIDDEN_FUNCS: Final = re.compile(
    r"\b(url|s3|s3Cluster|file|remote|remoteSecure|mysql|postgresql|hdfs|jdbc|odbc|"
    r"executable|azureBlobStorage|gcs|input)\s*\(",
    re.IGNORECASE,
)
_LIMIT_RE: Final = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_STRING_RE: Final = re.compile(r"'(?:[^'\\]|\\.)*'")
# Table references after FROM/JOIN. A subquery starts with "(" and does not
# match; a CTE name does, and is allowed by name.
_TABLE_REF_RE: Final = re.compile(
    r"\b(?:FROM|JOIN)\s+([`\"]?[A-Za-z_][A-Za-z0-9_.]*[`\"]?)", re.IGNORECASE
)
_CTE_RE: Final = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.IGNORECASE)


def validate_select(sql: str) -> str:
    """Return the bounded SQL, or raise ``ValidationError``.

    Keywords are checked with string literals blanked out, so a ``'DROP'`` in a
    WHERE clause is allowed and a real ``DROP`` hidden after a quote is not.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValidationError("run-query needs non-empty sql")
    if len(sql) > MAX_SQL_CHARS:
        raise ValidationError("sql too long", context={"max_chars": MAX_SQL_CHARS})
    text = sql.strip().rstrip(";").strip()
    bare = _STRING_RE.sub("''", text)
    if ";" in bare:
        raise ValidationError("only a single statement is allowed")
    if "--" in bare or "/*" in bare or "#" in bare:
        raise ValidationError("comments are not allowed in agent sql")
    head = bare.lstrip("( \n\t").split(None, 1)[0].upper() if bare else ""
    if head not in ("SELECT", "WITH"):
        raise ValidationError(
            "only SELECT or WITH ... SELECT is allowed", context={"head": head[:16]}
        )
    if match := _FORBIDDEN.search(bare):
        raise ValidationError(
            "statement keyword not allowed", context={"keyword": match.group(1).upper()}
        )
    if match := _FORBIDDEN_FUNCS.search(bare):
        raise ValidationError("table function not allowed", context={"function": match.group(1)})
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(bare)}
    for match in _TABLE_REF_RE.finditer(bare):
        ref = match.group(1).strip('`"')
        table = ref.split(".")[-1]
        if ref.lower() in ctes:
            continue
        if "." in ref or not table.startswith(TABLE_PREFIX):
            # The default database is shared with other producers; the agent
            # reads its own history, not theirs, and never system tables.
            raise ValidationError(
                "agent sql may only read aegis_ tables", context={"table": ref[:64]}
            )
    limits = [int(m.group(1)) for m in _LIMIT_RE.finditer(bare)]
    if not limits:
        return f"{text} LIMIT {DEFAULT_LIMIT}"
    if max(limits) > DEFAULT_LIMIT:
        raise ValidationError("LIMIT above the agent bound", context={"max_limit": DEFAULT_LIMIT})
    return text


class _RemoteClient(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


ClientFactory = Callable[[Settings], _RemoteClient]


def _default_factory(settings: Settings) -> _RemoteClient:
    # Imported here so this module loads even where the remote client is not
    # installed yet, and so tests can inject a fake without touching MCP.
    from aegis.mcp.remote import RemoteMCPClient

    client: _RemoteClient = RemoteMCPClient(
        settings.rawtree_mcp_url,
        settings.rawtree_read_key.get_secret_value(),
        allowlist=ALLOWLIST,
        timeout_s=settings.rawtree_timeout_s,
        name="rawtree",
    )
    return client


_FALLBACK_SPECS: Final = (
    ToolSpec(
        name="run-query",
        description=(
            "Run one read-only ClickHouse SELECT over the agent's history tables "
            "(aegis_metrics, aegis_agent_events, aegis_observations, aegis_memory_cards). "
            "Columns are Dynamic: "
            "cast with toString()/toFloat64OrNull(). LIMIT <= 200."
        ),
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="list-tables",
        description="List tables in the RawTree database with row counts.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="describe-table",
        description="Describe one table's columns.",
        input_schema={
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
            "additionalProperties": False,
        },
    ),
)


class RawTreeAgentTools:
    """Implements ``RemoteToolset`` for the brain's RawTree connection."""

    def __init__(self, settings: Settings, client_factory: ClientFactory | None = None) -> None:
        self._settings = settings
        self._factory = client_factory or _default_factory
        self._client: _RemoteClient | None = None
        self._specs: list[ToolSpec] = []

    @property
    def available(self) -> bool:
        return bool(self._settings.rawtree_read_key.get_secret_value().strip())

    def _remote(self) -> _RemoteClient:
        if not self.available:
            raise ValidationError("rawtree agent tools unavailable: read key not configured")
        if self._client is None:
            self._client = self._factory(self._settings)
        return self._client

    async def refresh(self) -> list[ToolSpec]:
        """Fetch the server's tool list, keeping only allowlisted names.

        Filtering here as well as in the remote client means a remote client
        that forgot its allowlist still cannot leak ``delete-table`` upward.
        """
        tools = await self._remote().list_tools()
        self._specs = [t for t in tools if t.name in ALLOWLIST]
        dropped = sorted({t.name for t in tools} - ALLOWLIST)
        if dropped:
            log.info("rawtree mcp tools filtered", dropped=dropped[:40])
        return self.tool_specs()

    def tool_specs(self) -> list[ToolSpec]:
        if not self.available:
            return []
        specs = self._specs or list(_FALLBACK_SPECS)
        return [
            ToolSpec(name=PREFIX + s.name, description=s.description, input_schema=s.input_schema)
            for s in specs
            if s.name in ALLOWLIST
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> QueryResult:
        bare = name.removeprefix(PREFIX)
        if not name.startswith(PREFIX) or bare not in ALLOWLIST:
            raise ValidationError(
                "tool is not on the rawtree allowlist", context={"tool": name[:64]}
            )
        args = dict(arguments)
        sql = ""
        if bare == "run-query":
            sql = validate_select(str(args.get("sql", "")))
            # The brain chooses the SQL, never the database or organisation.
            args = {"sql": sql}
            if self._settings.rawtree_database:
                args["database"] = self._settings.rawtree_database
        elif bare == "describe-table":
            table = args.get("table")
            if not isinstance(table, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,63}", table
            ):
                raise ValidationError("describe-table needs a plain table name")
            args = {"table": table}
        else:
            args = {}

        remote = self._remote()  # raises when unconfigured: that is a caller defect
        started = time.perf_counter()
        try:
            result = await remote.call_tool(bare, args)
        except AegisError as exc:
            return QueryResult(
                name="agent",
                sql=sql or bare,
                rows=[],
                source=Source.RAWTREE,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        rows = _result_rows(result)
        is_error = bool(getattr(result, "is_error", False))
        return QueryResult(
            name="agent",
            sql=sql or bare,
            rows=rows[:MAX_ROWS],
            source=Source.RAWTREE,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=(str(getattr(result, "text", ""))[:300] or "tool error") if is_error else None,
        )


def _result_rows(result: Any) -> list[dict[str, Any]]:
    """Rows from a ``RemoteCallResult``: structured first, then JSON text."""
    structured = getattr(result, "structured", None)
    payload: Any = structured
    if payload is None:
        text = getattr(result, "text", "")
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            return [{"text": str(text)[:4000]}]
    if isinstance(payload, dict):
        for key in ("data", "rows", "tables", "columns", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [v if isinstance(v, dict) else {"value": v} for v in value]
        table = payload.get("table")
        if isinstance(table, dict) and isinstance(table.get("columns"), list):
            return [c if isinstance(c, dict) else {"value": c} for c in table["columns"]]
        return [payload]
    if isinstance(payload, list):
        return [v if isinstance(v, dict) else {"value": v} for v in payload]
    return []


__all__ = ["ALLOWLIST", "PREFIX", "RawTreeAgentTools", "validate_select"]
