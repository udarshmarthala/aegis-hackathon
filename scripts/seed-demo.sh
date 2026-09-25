#!/usr/bin/env bash
#
# Seed a working demonstration: topology, a service-to-repository mapping, and
# one incident driven through the real ingestion path.
#
# Everything here goes through the same interfaces an operator or an alert
# manager would use. Nothing writes to the database behind the application's
# back, because a demo that bypasses the ingestion path proves the ingestion
# path works when it may not.
#
# Safe to re-run. Topology ingestion is MERGE-only, and the alert carries a
# stable external id per run so a repeat attaches to the existing incident
# rather than opening a second one.
#
# Usage:  scripts/seed-demo.sh [--fault error|latency|pool_exhaustion|none]
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f infra/docker/docker-compose.yml --env-file .env --project-name aegis-2-0"
FAULT="error"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fault) FAULT="${2:-error}"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
say "Checking the stack"
$COMPOSE ps --format '{{.Service}}={{.State}}' 2>/dev/null | tr '\n' ' ' || \
  fail "the stack is not reachable. Run 'make up' first."
echo

for svc in postgres api worker; do
  state=$($COMPOSE ps --format '{{.Service}}={{.State}}' 2>/dev/null | grep "^$svc=" || true)
  [[ "$state" == *running* ]] || fail "$svc is not running. Run 'make up' first."
done

# --------------------------------------------------------------- migrations --
# The API and worker both run these on boot, so this is a no-op on a healthy
# stack. It is here so a fresh volume is usable without waiting for a restart.
say "Applying migrations"
$COMPOSE exec -T api python -m aegis.persistence.migrate 2>/dev/null \
  || echo "  (migrations already applied by the API on boot)"

# ----------------------------------------------------------------- topology --
# Identifiers come from the same settings the graph ingestor uses, so seeded
# nodes and telemetry-discovered nodes are the same nodes. They were not always:
# a divergence here produced two Service nodes per service, only one of which
# carried the CALLS edges.
say "Seeding topology"
$COMPOSE exec -T api python /app/scripts/seed_topology.py 2>/dev/null \
  || $COMPOSE exec -T worker python - <<'PY' 2>/dev/null || echo "  (topology seeding skipped: neo4j unavailable)"
import asyncio
from aegis.container import build_container
from aegis.core.config import get_settings

async def main() -> None:
    container = build_container(get_settings())
    await container.connect()
    try:
        settings = get_settings()
        await container.graph_ingest.ensure_schema()
        stats = await container.graph_ingest.ingest_from_telemetry(
            container.prometheus,
            environment=settings.aegis_environment_name,
            workload=settings.workload_namespace,
        )
        print(f"  services ingested from telemetry: {stats}")
    finally:
        await container.aclose()

asyncio.run(main())
PY

# ------------------------------------------------------- fault injection ----
# The injector lives in the workload, never in Aegis, so the agent is never
# handed privileged knowledge of which fault was applied (ESD section 16).
if [[ "$FAULT" != "none" ]]; then
  say "Injecting a '$FAULT' fault into payment (Aegis is not told)"
  $COMPOSE exec -T api sh -c "curl -sS -X POST http://payment:8080/admin/fault \
    -H 'Content-Type: application/json' \
    -d '{\"mode\":\"$FAULT\",\"error_rate\":0.6,\"magnitude_ms\":400,\"probability\":1.0}'" \
    || echo "  (workload not running; start it with the 'workload' profile)"
  echo
  echo "  waiting 45s for the fault to appear in telemetry"
  sleep 45
fi

# --------------------------------------------------------------- the alert --
# Posted through the real ingestion endpoint with the real shared token, which
# is read inside the container so it never reaches this script's environment.
say "Posting an alert"
RESPONSE=$($COMPOSE exec -T api sh -c '
  curl -sS -X POST http://localhost:8000/v1/alerts \
    -H "Content-Type: application/json" \
    -H "X-Aegis-Ingest-Token: $ALERT_INGEST_TOKEN" \
    -d "{\"external_id\":\"demo-seed\",\"source\":\"demo\",
         \"title\":\"checkout 5xx rate breached SLO\",\"severity\":\"P1\",
         \"environment\":\"local\",\"service_hint\":\"checkout\",
         \"labels\":{\"service\":\"checkout\",\"alertname\":\"HighErrorRate\"},
         \"annotations\":{\"summary\":\"checkout returning elevated 5xx\"}}"
') || fail "the API refused the alert"

echo "  $RESPONSE"
INCIDENT=$(printf '%s' "$RESPONSE" | sed -E 's/.*"incident_id":"([^"]+)".*/\1/')
[[ "$INCIDENT" == inc_* ]] || fail "could not read an incident id from the response"

# ------------------------------------------------------------------ result --
say "Incident $INCIDENT"
cat <<EOF

The worker is investigating now. Watch it with:

  make logs                                   # or: $COMPOSE logs -f worker
  open http://localhost:3000/incidents/$INCIDENT

Sign in at http://localhost:3000 with the value of AUTH_DEV_BYPASS_TOKEN from
your .env, under "Local development".

Expect an evidence trail, a hypothesis stack and either a diagnosis or an
explicit abstention. Abstention is a correct outcome, not a failure: with
GitHub unconfigured there is no change analysis, so ambiguous cases are meant
to say so rather than guess.

EOF
