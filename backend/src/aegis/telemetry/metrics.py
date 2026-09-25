"""Aegis's own Prometheus metrics.

Aegis has always scraped everyone else. It exposed nothing about itself: the
``aegis-api`` job in ``infra/prometheus/prometheus.yml`` has been pointed at
``/metrics`` on a service that never served it, so the target has been down
since the scrape config was written.

That gap matters more here than in an ordinary service. A platform that takes
autonomous action on production has to answer questions about its own conduct -
how many actions it executed, how many a human blocked, how often verification
came back unmeasurable - and it has to answer them from the outside, with
numbers an operator can alert on rather than a page they have to read.

What is deliberately NOT here:

* **No per-incident labels.** Incident ids are unbounded, and an unbounded label
  value is how a metrics backend falls over. Incident detail belongs in
  Postgres, which is queryable and which nobody pages on.
* **No model output.** Metrics count events; they never carry free text.
"""

from __future__ import annotations

from typing import Any, Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from aegis.core.logging import get_logger

log = get_logger(__name__)

# A dedicated registry rather than the global default. Two app instances in one
# test process would otherwise collide on duplicate collector registration, and
# the failure mode is an import-time crash that looks nothing like its cause.
REGISTRY: Final = CollectorRegistry()

# ---- ingestion and incidents --------------------------------------------- #

alerts_received = Counter(
    "aegis_alerts_received_total",
    "Alerts accepted at the ingestion boundary.",
    ["source", "severity", "deduplicated"],
    registry=REGISTRY,
)

incidents_opened = Counter(
    "aegis_incidents_opened_total",
    "Incidents created from an alert.",
    ["environment", "severity"],
    registry=REGISTRY,
)

incident_state_changes = Counter(
    "aegis_incident_state_changes_total",
    "Incident state transitions.",
    ["from_state", "to_state"],
    registry=REGISTRY,
)

# ---- investigation -------------------------------------------------------- #

investigations = Counter(
    "aegis_investigations_total",
    "Completed investigation runs by outcome.",
    ["outcome"],  # diagnosed | abstained | budget_exhausted | failed
    registry=REGISTRY,
)

investigation_duration = Histogram(
    "aegis_investigation_duration_seconds",
    "Wall-clock duration of an investigation.",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200),
    registry=REGISTRY,
)

evidence_recorded = Counter(
    "aegis_evidence_recorded_total",
    "Evidence items written, by source and trust class.",
    ["source_type", "trust_class"],
    registry=REGISTRY,
)

evidence_gaps = Counter(
    "aegis_evidence_gaps_total",
    "Sources that could not be consulted. Never the same as finding nothing.",
    ["source"],
    registry=REGISTRY,
)

llm_calls = Counter(
    "aegis_llm_calls_total",
    "Model calls by provider and outcome.",
    ["provider", "outcome"],  # ok | failed | unavailable
    registry=REGISTRY,
)

tool_calls = Counter(
    "aegis_tool_calls_total",
    "Tool invocations through the MCP boundary.",
    ["tool", "access", "outcome"],  # ok | degraded | refused | failed
    registry=REGISTRY,
)

# ---- safety: the numbers an operator actually pages on --------------------- #

actions_proposed = Counter(
    "aegis_actions_proposed_total",
    "Remediation actions proposed by an agent.",
    ["action_type", "risk_tier"],
    registry=REGISTRY,
)

policy_decisions = Counter(
    "aegis_policy_decisions_total",
    "Policy outcomes, labelled with the rule that decided them.",
    ["effect", "risk_tier", "matched_rule"],
    registry=REGISTRY,
)

actions_executed = Counter(
    "aegis_actions_executed_total",
    "Actions that reached the environment, and whether a human authorised them.",
    ["action_type", "autonomous", "outcome"],  # success | failed | rolled_back
    registry=REGISTRY,
)

rollbacks = Counter(
    "aegis_rollbacks_total",
    "Rollback attempts and their result. A failed rollback is the worst state "
    "the system can reach and deserves its own alert.",
    ["action_type", "outcome"],  # succeeded | failed
    registry=REGISTRY,
)

verifications = Counter(
    "aegis_verifications_total",
    "Verification runs by verdict. UNAVAILABLE is counted separately from "
    "FAILED because an unmeasurable claim is not a failing one.",
    ["verdict"],
    registry=REGISTRY,
)

approvals_requested = Counter(
    "aegis_approvals_requested_total",
    "Approval requests opened for a human.",
    ["action_type"],
    registry=REGISTRY,
)

approvals_decided = Counter(
    "aegis_approvals_decided_total",
    "Human approval decisions. 'expired' is never recorded as 'rejected' - "
    "nobody rejected it.",
    ["decision"],  # approved | rejected | more_evidence | expired
    registry=REGISTRY,
)

lease_conflicts = Counter(
    "aegis_lease_conflicts_total",
    "Attempts to act on a resource another worker already held.",
    registry=REGISTRY,
)

# ---- posture: gauges an alert rule can assert on --------------------------- #

autonomy_enabled = Gauge(
    "aegis_autonomy_enabled",
    "1 when autonomous execution is permitted by configuration.",
    registry=REGISTRY,
)

kill_switch_engaged = Gauge(
    "aegis_kill_switch_engaged",
    "1 when any kill switch is engaged. A degraded policy store also reports 1, "
    "because an unreadable store fails closed.",
    registry=REGISTRY,
)

audit_write_failures = Gauge(
    "aegis_audit_write_failures",
    "Audit rows that could not be written. Non-zero means the trail has holes "
    "and any completeness claim about it is now qualified.",
    registry=REGISTRY,
)

open_incidents = Gauge(
    "aegis_open_incidents",
    "Incidents not in a terminal state.",
    ["severity"],
    registry=REGISTRY,
)

queue_depth = Gauge(
    "aegis_queue_depth",
    "Workflow jobs waiting to be claimed.",
    ["kind"],
    registry=REGISTRY,
)

circuit_breaker_open = Gauge(
    "aegis_circuit_breaker_open",
    "1 when a dependency's breaker is open and Aegis is deliberately not "
    "calling it.",
    ["dependency"],
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Serialise the registry for the scrape endpoint.

    The classic Prometheus text exposition format, paired with its own content
    type. Declaring the OpenMetrics content type while emitting this format is
    a real and easily-missed bug: OpenMetrics requires a trailing ``# EOF``
    sentinel, and Prometheus rejects the whole scrape with "data does not end
    with # EOF" - which reads like a network fault rather than a formatting
    one.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def observe_breakers(states: dict[str, str]) -> None:
    """Mirror the in-process breaker states onto gauges.

    Refreshed at scrape time rather than on every state change: a breaker that
    opens and closes rapidly would otherwise generate more metric churn than
    signal, and the scrape is the only moment the value is read anyway.
    """
    for dependency, state in states.items():
        circuit_breaker_open.labels(dependency=dependency).set(
            1.0 if state == "open" else 0.0
        )


def observe_posture(*, autonomy: bool, kill_switch: bool, audit_failures: int) -> None:
    """Refresh the safety-posture gauges."""
    autonomy_enabled.set(1.0 if autonomy else 0.0)
    kill_switch_engaged.set(1.0 if kill_switch else 0.0)
    audit_write_failures.set(float(audit_failures))


def safe(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Record a metric without ever letting it break the caller.

    Observability is not a control-plane dependency (CLAUDE.md 9). A metric that
    could not be recorded is a lost data point; an exception escaping into a
    remediation path is an outage.
    """
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - metrics never propagate
        log.debug("metric not recorded", error=str(exc))


__all__ = [
    "REGISTRY",
    "actions_executed",
    "actions_proposed",
    "alerts_received",
    "approvals_decided",
    "approvals_requested",
    "audit_write_failures",
    "autonomy_enabled",
    "circuit_breaker_open",
    "evidence_gaps",
    "evidence_recorded",
    "incident_state_changes",
    "incidents_opened",
    "investigation_duration",
    "investigations",
    "kill_switch_engaged",
    "lease_conflicts",
    "llm_calls",
    "observe_breakers",
    "observe_posture",
    "open_incidents",
    "policy_decisions",
    "queue_depth",
    "render",
    "rollbacks",
    "safe",
    "tool_calls",
    "verifications",
]
