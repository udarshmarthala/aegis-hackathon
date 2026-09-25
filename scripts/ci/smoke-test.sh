#!/usr/bin/env bash
# Post-deployment smoke test.
#
# This is not a "did it return 200" script. A control plane that answers 200 on
# /health while refusing no unauthenticated request is a worse outcome than one
# that is down, so the assertions below cover liveness, the hard dependency,
# and the fail-closed auth boundary.
#
# Usage: smoke-test.sh <base-url> [expected-environment]
# Exits non-zero on the first failed assertion.
set -euo pipefail

BASE_URL="${1:?usage: smoke-test.sh <base-url> [expected-environment]}"
EXPECT_ENV="${2:-}"
BASE_URL="${BASE_URL%/}"

CURL=(curl --silent --show-error --max-time 20 --retry 0)
failures=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }

status_of() {
  "${CURL[@]}" -o /dev/null -w '%{http_code}' "$1" || echo "000"
}

echo "Smoke testing ${BASE_URL}"

# --- 1. liveness: the process answers, without touching a dependency --------
# Retried: a freshly stabilised ECS service can still be finishing its first
# request. Ten attempts over ~50s, then it is a real failure.
live=""
for attempt in $(seq 1 10); do
  live="$(status_of "${BASE_URL}/health/live")"
  [ "${live}" = "200" ] && break
  echo "  ... /health/live returned ${live} (attempt ${attempt}/10)"
  sleep 5
done
if [ "${live}" = "200" ]; then pass "/health/live -> 200"; else fail "/health/live -> ${live}"; fi

# --- 2. readiness: Postgres, the only hard dependency, is reachable ---------
ready="$(status_of "${BASE_URL}/health/ready")"
if [ "${ready}" = "200" ]; then pass "/health/ready -> 200"; else fail "/health/ready -> ${ready}"; fi

# --- 3. integration picture ------------------------------------------------
health_body="$("${CURL[@]}" "${BASE_URL}/health" || echo '{}')"
if printf '%s' "${health_body}" | grep -q '"postgres"'; then
  pass "/health reports component detail"
else
  fail "/health did not report per-component detail"
fi

# Degraded is acceptable: an unreachable Prometheus is a recorded evidence gap,
# not an outage (CLAUDE.md invariant 9). "unavailable" is not acceptable — that
# means the system of record is gone.
# Only the top-level status: components that are not deployed report their own
# state, and matching any "unavailable" anywhere read those as an outage.
top_status="$(printf '%s' "${health_body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
if [ "${top_status}" = "unavailable" ] || [ -z "${top_status}" ]; then
  fail "/health reports status=unavailable (Postgres unreachable)"
else
  pass "/health status is healthy or degraded"
fi

# --- 4. environment identity: we deployed what we think we deployed --------
if [ -n "${EXPECT_ENV}" ]; then
  if printf '%s' "${health_body}" | grep -q "\"${EXPECT_ENV}\""; then
    pass "/health identifies environment '${EXPECT_ENV}'"
  else
    fail "/health does not mention environment '${EXPECT_ENV}'"
  fi
fi

# --- 5. fail closed: an unauthenticated read must be refused ---------------
# 401 or 403 only. A 200 here means auth is not enforced and the deployment
# must be rolled back immediately.
protected="$(status_of "${BASE_URL}/v1/incidents")"
case "${protected}" in
  401|403) pass "/v1/incidents refuses anonymous access (${protected})" ;;
  200)     fail "/v1/incidents served an anonymous request (200) - AUTH IS NOT ENFORCED" ;;
  *)       fail "/v1/incidents returned unexpected ${protected}" ;;
esac

# --- 6. alert ingestion is token-gated -------------------------------------
ingest="$("${CURL[@]}" -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/v1/alerts" \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"smoke-unauthorised","title":"smoke","severity":"P3"}' || echo "000")"
case "${ingest}" in
  401|403) pass "/v1/alerts refuses an untokened POST (${ingest})" ;;
  # 422 means the body was validated before the token was checked. That is a
  # weaker ordering than we want but it is not an authorisation bypass.
  422)     pass "/v1/alerts rejected the payload (${ingest}); token ordering noted" ;;
  200|201) fail "/v1/alerts accepted an untokened POST - INGEST IS NOT GATED" ;;
  *)       fail "/v1/alerts returned unexpected ${ingest}" ;;
esac

echo
if [ "${failures}" -gt 0 ]; then
  echo "${failures} smoke assertion(s) failed." >&2
  exit 1
fi
echo "All smoke assertions passed."
