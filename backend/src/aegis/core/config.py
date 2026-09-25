"""Configuration, validated once at import.

Aegis fails closed. A malformed autonomy mode, a missing database password or an
unparseable budget raises ``ConfigError`` during boot rather than silently
choosing a permissive default at 3am.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aegis.core.errors import ConfigError


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class AutonomyMode(StrEnum):
    OFF = "off"          # observe only; no write action of any tier
    GUARDED = "guarded"  # tier-1 allowlist only, all 8 gates enforced
    STANDARD = "standard"


Port = Annotated[int, Field(ge=1, le=65535)]
Positive = Annotated[int, Field(gt=0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env", "../../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- identity ---
    aegis_env: Environment = Environment.LOCAL
    aegis_environment_name: str = "local-docker"
    aegis_version: str = "2.0.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- api ---
    api_host: str = "0.0.0.0"  # noqa: S104 - containerised, bound by compose
    api_port: Port = 8000
    api_public_url: str = "http://localhost:8000"
    cors_allowed_origins: str = "http://localhost:3000"
    alert_ingest_token: SecretStr = SecretStr("")
    internal_signing_key: SecretStr = SecretStr("")

    # --- postgres (the only hard dependency) ---
    postgres_host: str = "postgres"
    postgres_port: Port = 5432
    postgres_db: str = "aegis"
    postgres_user: str = "aegis"
    postgres_password: SecretStr = SecretStr("")
    postgres_pool_min: Positive = 2
    postgres_pool_max: Positive = 16
    postgres_statement_timeout_ms: Positive = 15_000

    # --- redis (soft) ---
    redis_host: str = "redis"
    redis_port: Port = 6379
    redis_password: SecretStr = SecretStr("")
    redis_db: int = 0

    # --- neo4j (soft) ---
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"

    # --- evidence sources (all soft) ---
    prometheus_url: str = "http://prometheus:9090"
    tempo_url: str = "http://tempo:3200"
    loki_url: str = "http://loki:3100"
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "aegis-api"
    otel_traces_enabled: bool = True
    source_timeout_s: float = 10.0

    # --- llm (Google AI Studio only) ---
    #
    # One provider, several keys. Multi-provider routing was removed because it
    # bought nothing real: the fallback was never a different *capability*, only
    # a different account, and carrying four provider dialects meant a model id
    # valid for one endpoint 404ing on another - which disabled the fallback at
    # exactly the moment the primary was failing.
    #
    # Free-tier Gemini keys are rate-limited per key and per day, so the useful
    # axis of redundancy is the key, not the vendor. Keys are tried in order and
    # a quota-exhausted key is parked, not retried.
    google_api_key: SecretStr = SecretStr("")
    google_api_key_2: SecretStr = SecretStr("")
    google_api_key_3: SecretStr = SecretStr("")
    google_api_key_4: SecretStr = SecretStr("")
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # Model availability changes over time, so these stay configuration rather
    # than constants baked into the router. A ``vendor/model`` prefix left over
    # from an aggregator is stripped: the native endpoint rejects it.
    # One model across all three task classes, so a key failover cannot change
    # the answer's characteristics - only which quota paid for it. Chosen by
    # probing the live endpoint: 2.5-flash is 404 for new keys, 3.5-flash and
    # 3.8-flash returned 503 under load, 3.6-flash answered in ~4s.
    llm_model_fast: str = "gemini-3.6-flash"
    llm_model_reasoning: str = "gemini-3.6-flash"
    llm_model_code: str = "gemini-3.6-flash"
    # gemini-embedding-001 emits 3072 dimensions unless asked otherwise. The
    # corpus columns are vector(1536) and migrations are forward-only, so the
    # width is requested explicitly per call rather than accepted as returned.
    llm_embedding_model: str = "gemini-embedding-001"
    llm_embedding_dim: Positive = 1536
    # Structured agent outputs are small; a modest cap avoids providers
    # rejecting a request whose reserved budget exceeds the account balance.
    llm_max_output_tokens: Positive = 8192
    llm_request_timeout_s: float = 90.0
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # --- langsmith (never a control-plane dependency) ---
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "aegis-2.0"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # --- auth ---
    firebase_project_id: str = ""
    firebase_service_account_path: str = ""
    auth_dev_mode: bool = False
    auth_dev_bypass_token: SecretStr = SecretStr("")

    # --- slack ---
    # Read here rather than from os.environ inside the client, so that the
    # whole of Aegis's configuration is visible in one typed object and a
    # test can construct a configured client without mutating the process
    # environment.
    slack_bot_token: SecretStr = SecretStr("")
    slack_webhook_url: SecretStr = SecretStr("")
    slack_default_channel: str = ""

    # --- github ---
    github_token: SecretStr = SecretStr("")
    github_default_owner: str = ""
    github_api_url: str = "https://api.github.com"

    # --- agent budgets (hard caps) ---
    agent_max_wall_seconds: Positive = 600
    agent_max_llm_calls: Positive = 40
    agent_max_tool_calls: Positive = 80
    agent_max_tokens: Positive = 400_000
    agent_max_parallel_investigators: Annotated[int, Field(ge=1, le=8)] = 4
    agent_hypothesis_loop_limit: Annotated[int, Field(ge=1, le=10)] = 4

    # --- autonomy / safety ---
    autonomy_enabled: bool = False
    autonomy_mode: AutonomyMode = AutonomyMode.GUARDED
    autonomy_allowed_tiers: str = "1"
    # Services eligible for autonomous action in production. Policy rule
    # `production_allowlist` tests membership, and the gate chain previously
    # passed an empty frozenset with no configuration source - which made every
    # production proposal naming a service require a human. That failed safe,
    # but it also meant the autonomous path could never be exercised or tested
    # in production at all. Empty still means "nothing is allowlisted", which
    # remains the correct default.
    autonomy_service_allowlist: str = ""
    autonomy_max_actions_per_hour: Positive = 10
    approval_ttl_seconds: Positive = 900
    resource_lease_ttl_seconds: Positive = 300

    # --- sandbox ---
    sandbox_enabled: bool = True
    sandbox_image: str = "aegis/sandbox-python:2.0"
    sandbox_cpu_limit: float = 2.0
    sandbox_memory_limit: str = "2g"
    sandbox_wall_clock_limit_s: Positive = 300
    sandbox_network: str = "none"
    sandbox_docker_host: str = "unix:///var/run/docker.sock"

    # --- evaluation ---
    # Where the benchmark catalogue is read from. Only scenario *metadata*
    # (id, title, category, workload, difficulty) ever leaves this directory
    # through the API; ground truth is loaded, used for scoring inside the
    # process and never serialised out of it (evaluation.schema.assert_sealed).
    # A missing directory is reported as "catalogue unavailable", which is a
    # different answer from "there are no scenarios".
    eval_scenarios_dir: str = "eval/scenarios"
    # Hard cap on how much of the catalogue one API call may render. The loader
    # has its own file cap; this bounds the response as well.
    eval_catalogue_limit: Annotated[int, Field(ge=1, le=500)] = 200

    # --- workload under observation ---
    workload_adapter: Literal["compose", "kubernetes", "ecs"] = "compose"
    workload_namespace: str = "aegis-workload"
    kube_context: str = "kind-aegis"
    aws_region: str = "us-east-1"
    ecs_cluster: str = ""

    # --- long-horizon agent: mode and driver ---
    # ``scripted`` runs the golden path with no network at all: the scripted
    # brain, the rule compactor and fixture-backed sponsors. ``live`` tries the
    # real providers first and falls back tier by tier, labelling each fallback.
    aegis_mode: Literal["live", "scripted"] = "scripted"
    # Which orchestrator drives an investigation job. Both stay in the tree; the
    # horizon step loop is the default incident driver.
    horizon_driver: Literal["horizon", "langgraph"] = "horizon"
    horizon_max_steps: Annotated[int, Field(ge=4, le=500)] = 80
    horizon_step_timeout_s: float = Field(default=120.0, gt=0)
    verification_sustained_samples: Annotated[int, Field(ge=1, le=30)] = 5
    verification_sample_interval_s: float = Field(default=3.0, gt=0)

    # --- Bedrock (the brain) ---
    aws_profile: str = ""
    aws_access_key_id: SecretStr = SecretStr("")
    aws_secret_access_key: SecretStr = SecretStr("")
    # Verified against ``aws bedrock list-inference-profiles``. The fallback id is
    # a second Bedrock model tried when the primary is denied for the account
    # (403), so the brain stays on Bedrock rather than dropping a whole tier.
    bedrock_model_id: str = ""
    bedrock_fallback_model_id: str = ""
    bedrock_timeout_s: float = Field(default=60.0, gt=0)
    bedrock_max_tokens: Positive = 4096

    # --- Gemini key pools (N keys, two named pools) ---
    # Slots are 1-based: GOOGLE_API_KEY is slot 1, GOOGLE_API_KEY_2..8 follow.
    # Each pool names the slots it may use, so a Bedrock outage that pushes the
    # brain onto Gemini cannot starve compaction of quota.
    google_api_key_5: SecretStr = SecretStr("")
    google_api_key_6: SecretStr = SecretStr("")
    google_api_key_7: SecretStr = SecretStr("")
    google_api_key_8: SecretStr = SecretStr("")
    gemini_compactor_keys: str = "1,2,3,4,5"
    gemini_brain_keys: str = "6,7,8"

    # --- RawTree (episodic memory, heartbeat, agent history) ---
    rawtree_write_key: SecretStr = SecretStr("")
    rawtree_read_key: SecretStr = SecretStr("")
    rawtree_database: str = ""
    rawtree_api_url: str = "https://api.rawtree.com"
    rawtree_mcp_url: str = "https://mcp.rawtree.com/mcp"
    rawtree_flush_interval_ms: Positive = 500
    rawtree_queue_max: Positive = 5000
    rawtree_timeout_s: float = Field(default=8.0, gt=0)

    # --- Nimble (external evidence) ---
    nimble_api_key: SecretStr = SecretStr("")
    nimble_mcp_url: str = "https://mcp.nimbleway.com/mcp"
    # Measured live: MCP handshake ~3 s plus search ~5 s, and an extract adds
    # ~4 s. An 8 s bound would make the fixture the answer almost every time.
    nimble_timeout_s: float = Field(default=20.0, gt=0)

    # --- Black Forest Labs (incident map) ---
    bfl_api_key: SecretStr = SecretStr("")
    bfl_api_url: str = "https://api.bfl.ai/v1"
    bfl_model: str = "flux-pro-1.1"
    bfl_timeout_s: float = Field(default=60.0, gt=0)

    # --- heartbeat and metrics forwarder (run inside the worker) ---
    heartbeat_interval_s: float = Field(default=5.0, gt=0)
    heartbeat_zscore_threshold: float = Field(default=3.0, gt=0)
    forwarder_interval_s: float = Field(default=5.0, gt=0)
    # service=url pairs scraped by the forwarder; the workload exposes
    # Prometheus text on /metrics.
    workload_metrics_targets: str = (
        "gateway=http://gateway:8080/metrics,"
        "checkout=http://checkout:8080/metrics,"
        "payment=http://payment:8080/metrics"
    )

    # ------------------------------------------------------------------ #
    # Derived accessors                                                   #
    # ------------------------------------------------------------------ #

    @property
    def postgres_dsn(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        pw = self.redis_password.get_secret_value()
        auth = f":{pw}@" if pw else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def allowed_tiers(self) -> frozenset[int]:
        """Risk tiers eligible for autonomous execution.

        Empty when autonomy is disabled - the policy engine then requires a human
        for every write, which is the fail-closed default.
        """
        if not self.autonomy_enabled or self.autonomy_mode is AutonomyMode.OFF:
            return frozenset()
        out: set[int] = set()
        for part in self.autonomy_allowed_tiers.split(","):
            part = part.strip()
            if not part:
                continue
            out.add(int(part))
        return frozenset(out)

    @property
    def service_allowlist(self) -> frozenset[str]:
        """Services permitted to receive an autonomous action in production.

        Parsed from a comma-separated list. Outside production the allowlist is
        not consulted by the policy engine, so an empty value here never blocks
        local or staging work.
        """
        return frozenset(
            part.strip() for part in self.autonomy_service_allowlist.split(",") if part.strip()
        )

    @property
    def google_api_keys(self) -> tuple[str, ...]:
        """Every configured Gemini key, in priority order, deduplicated.

        Order is the failover order: the first is primary and the rest are
        tried in turn when a key is exhausted. Empty slots are skipped so an
        operator can configure one key or four without changing anything else.

        Deduplicated because the same key pasted twice is redundancy that does
        not exist, and it would make a single exhausted quota look like two
        healthy accounts.
        """
        ordered = (
            self.google_api_key,
            self.google_api_key_2,
            self.google_api_key_3,
            self.google_api_key_4,
        )
        seen: list[str] = []
        for secret in ordered:
            value = secret.get_secret_value().strip()
            if value and value not in seen:
                seen.append(value)
        return tuple(seen)

    def _google_slot_secrets(self) -> tuple[SecretStr, ...]:
        """All eight key slots, slot 1 first. Empty slots stay in place."""
        return (
            self.google_api_key,
            self.google_api_key_2,
            self.google_api_key_3,
            self.google_api_key_4,
            self.google_api_key_5,
            self.google_api_key_6,
            self.google_api_key_7,
            self.google_api_key_8,
        )

    def gemini_pool_keys(self, pool: Literal["compactor", "brain"]) -> tuple[str, ...]:
        """The configured keys for one named pool, in slot order, deduplicated.

        A slot named by the pool but left empty is skipped, so an operator can
        start with two keys and add the rest later without touching the pool
        lists. Deduplicated for the same reason as ``google_api_keys``: one key
        pasted twice is not two quotas.
        """
        spec = self.gemini_compactor_keys if pool == "compactor" else self.gemini_brain_keys
        secrets = self._google_slot_secrets()
        seen: list[str] = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            value = secrets[int(part) - 1].get_secret_value().strip()
            if value and value not in seen:
                seen.append(value)
        return tuple(seen)

    @property
    def metrics_targets(self) -> dict[str, str]:
        """``service -> /metrics URL`` for the RawTree forwarder."""
        out: dict[str, str] = {}
        for part in self.workload_metrics_targets.split(","):
            if "=" not in part:
                continue
            service, url = part.split("=", 1)
            if service.strip() and url.strip():
                out[service.strip()] = url.strip()
        return out

    @property
    def is_production(self) -> bool:
        return self.aegis_env is Environment.PRODUCTION

    # ------------------------------------------------------------------ #
    # Validation                                                          #
    # ------------------------------------------------------------------ #

    @field_validator("autonomy_allowed_tiers")
    @classmethod
    def _tiers_parse(cls, v: str) -> str:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or not 0 <= int(part) <= 3:
                raise ValueError(f"autonomy tier {part!r} must be an integer 0-3")
            if int(part) == 3:
                raise ValueError("tier 3 can never be autonomous (PRD FR-12)")
        return v

    @field_validator("gemini_compactor_keys", "gemini_brain_keys")
    @classmethod
    def _pool_slots_parse(cls, v: str) -> str:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or not 1 <= int(part) <= 8:
                raise ValueError(f"gemini key slot {part!r} must be an integer 1-8")
        return v

    @model_validator(mode="after")
    def _pool_bounds(self) -> Settings:
        if self.postgres_pool_max < self.postgres_pool_min:
            raise ValueError("postgres_pool_max must be >= postgres_pool_min")
        return self

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        """Refuse to start in production with development affordances enabled."""
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.auth_dev_mode:
            problems.append("auth_dev_mode must be false in production")
        if "*" in self.cors_allowed_origins:
            problems.append("wildcard CORS origin is not allowed in production")
        if not self.alert_ingest_token.get_secret_value():
            problems.append("alert_ingest_token is required in production")
        if not self.firebase_project_id:
            problems.append("firebase_project_id is required in production")
        if not self.postgres_password.get_secret_value():
            problems.append("postgres_password is required in production")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _llm_configured(self) -> Settings:
        """At least one Gemini key must exist in production.

        Locally an unconfigured model is a legitimate state: investigations
        still collect evidence and then abstain, which is the designed
        behaviour rather than a crash.
        """
        if not self.google_api_keys and self.is_production:
            raise ValueError("no GOOGLE_API_KEY is configured")
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that configuration is parsed and validated exactly once. Any
    failure is re-raised as ConfigError, which callers treat as fatal.
    """
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(
            "invalid configuration - refusing to start",
            context={"errors": exc.errors(include_url=False)},
        ) from exc
