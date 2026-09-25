# PRD.md — Aegis AI SRE Platform
## Product Requirements Document

**Document status:** Architecture baseline / product reset  
**Product:** Aegis  
**Version:** 2.0 design baseline  
**Primary objective:** Build a production-grade AI SRE platform that can detect, investigate, debug, repair, verify, communicate, and safely remediate incidents in distributed systems.  
**Initial reference workloads:** DeathStarBench SocialNetwork and Hotel Reservation  
**Initial execution environment:** Docker + Kind for the local production simulator; Amazon ECS/Fargate as the first cloud container target  
**AI observability/evaluation:** LangSmith  
**Infrastructure observability:** OpenTelemetry-centered  
**Knowledge layer:** Neo4j knowledge graph + PostgreSQL + vector retrieval  
**Agent orchestration:** LangGraph  
**Tool boundary:** MCP

---

# 1. Executive Summary

Aegis is an AI-native Site Reliability Engineering platform.

The product is not intended to be another chatbot that explains logs, nor a dashboard that merely wraps Prometheus and Grafana. Its job is to take responsibility for the operational investigation loop that currently consumes an SRE's time:

**Detect → Triage → Understand → Correlate → Diagnose → Debug → Repair → Verify → Communicate → Prevent**

Aegis observes a distributed application through standardized telemetry and operational integrations. When an incident occurs, Aegis builds a structured incident context, maps the affected system through an operational knowledge graph, gathers evidence using bounded tools, forms and tests hypotheses, inspects source code and changes, and can generate a candidate repair.

Repairs are not considered successful because an LLM claims that they are correct. Aegis must prove them through deterministic checks and execution-based verification. Candidate changes are tested in an isolated environment, validated against the original failure, and then passed through a policy/risk engine.

Low-risk operations may be autonomously executed in production when explicitly allowed by policy. Higher-risk actions require human approval. Unsafe or ambiguous actions are blocked and escalated.

The platform must also measure itself. Every significant AI operation is traced in LangSmith, and every release of prompts, models, agent logic, retrievers, policies, or tools is evaluated against a fixed incident benchmark with ground-truth labels. Aegis therefore becomes an engineering system that can demonstrate whether it is actually improving reliability rather than merely producing compelling prose.

---

# 2. Product Vision

## Vision statement

**Aegis is an AI SRE that understands the production system, proves what went wrong, fixes what it can safely fix, and knows when to stop.**

The ideal outcome is not "AI replaces SREs."

The ideal outcome is:

> The SRE is only interrupted when human judgment materially adds value.

Aegis should absorb repetitive investigation, evidence gathering, code navigation, incident coordination, routine remediation, and post-incident knowledge capture while keeping human control over consequential decisions.

---

# 3. Problem Statement

Modern distributed applications generate enormous operational context:

- logs
- metrics
- traces
- deployment history
- source code
- pull requests
- feature flags
- configuration
- dependency graphs
- infrastructure state
- runbooks
- incident history
- ownership metadata
- alerts
- communication threads

The problem is not lack of data.

The problem is that the data is fragmented across systems, changes continuously, and requires an experienced engineer to correlate it under time pressure.

During an incident, an SRE typically needs to answer:

1. What is broken?
2. When did it start?
3. Which service is the first failing component?
4. Is this a local failure or propagation from another dependency?
5. What changed before the incident?
6. Which source-code path could explain the behavior?
7. What customers or business functions are affected?
8. Have we seen this before?
9. What is the safest available mitigation?
10. How do we know the fix worked?
11. What should be changed so this does not happen again?

Aegis addresses this as an evidence-driven engineering problem.

---

# 4. Product Principles

## 4.1 Evidence over confidence

LLM confidence is not operational evidence.

Every important claim must reference evidence and expose its provenance.

## 4.2 Execution proves debugging

A diagnosis is stronger when the failure can be reproduced and a candidate fix causes the expected recovery.

## 4.3 The agent is not the authority

The LLM proposes. Deterministic systems validate. Policy decides. Execution systems enforce.

## 4.4 Read broadly, write narrowly

Aegis may require broad read access across telemetry and code systems. Write access must be narrowly scoped to explicitly allowed operations.

## 4.5 Abstention is a valid outcome

"Insufficient evidence" is better than fabricated certainty.

## 4.6 Evaluation is part of the product

AI behavior must be benchmarked continuously. New models or prompts cannot silently change operational behavior.

## 4.7 Environment portability

The application being monitored is separate from the infrastructure on which it runs.

Initial environments:

- Kind
- ECS/Fargate

Future adapters may include:

- EKS
- GKE
- AKS
- other container platforms

---

# 5. Target Users

## 5.1 Primary: On-call SRE / Platform Engineer

Needs:
- fast incident understanding
- evidence without dashboard hopping
- dependency awareness
- safe mitigation
- precise code-level debugging
- reliable escalation

Success:
- less context switching
- lower investigation time
- fewer repetitive actions
- fewer avoidable pages

## 5.2 Incident Commander

Needs:
- incident state
- blast radius
- confidence and uncertainty
- live timeline
- communication automation
- approval controls

Success:
- less coordination overhead
- accurate stakeholder updates
- clearer decision history

## 5.3 Engineering Manager / Service Owner

Needs:
- recurring incident patterns
- affected services
- change risk
- remediation effectiveness
- postmortem quality

Success:
- fewer repeated incidents
- actionable reliability work

## 5.4 Platform / Security Administrator

Needs:
- action policies
- access boundaries
- audit trail
- kill switch
- autonomous-action controls
- compliance evidence

Success:
- autonomy without uncontrolled infrastructure access

---

# 6. Product Scope

Aegis is organized into six operational loops.

## Loop A — Detect

Inputs:
- Prometheus/Alertmanager alerts
- application telemetry
- infrastructure health signals
- deployment events
- customer-impact signals when available

Capabilities:
- ingestion
- deduplication
- correlation
- severity
- incident creation

## Loop B — Understand

Capabilities:
- service localization
- temporal analysis
- trace analysis
- dependency graph traversal
- recent-change analysis
- blast-radius estimation
- historical incident retrieval

## Loop C — Debug

Capabilities:
- source-code retrieval
- commit/diff analysis
- stack trace analysis
- failing request reproduction
- test generation
- candidate patch generation
- sandbox execution

## Loop D — Repair

Capabilities:
- safe mitigation
- configuration adjustment
- service restart
- controlled scaling
- rollback proposal
- code fix pipeline
- staged rollout

## Loop E — Verify

Capabilities:
- reproduce original fault
- run regression tests
- compare pre/post telemetry
- detect collateral regressions
- validate health
- verify target outcome
- rollback if validation fails

## Loop F — Learn / Prevent

Capabilities:
- incident memory
- recurring-incident detection
- reliability recommendations
- risk analysis
- postmortem generation
- runbook improvement suggestions
- alert quality recommendations

---

# 7. Initial Reference Environment

Aegis will initially operate against two DeathStarBench workloads:

1. SocialNetwork
2. Hotel Reservation

Both are deployed as containerized distributed applications.

For local development:

```text
Docker images
    ↓
Kind
    ↓
DeathStarBench workloads
```

The local environment is intentionally treated as a production simulator rather than as the product itself.

It must support:

- realistic request traffic
- distributed service dependencies
- service failures
- latency injection
- error injection
- resource pressure
- network faults
- dependency failures
- configuration mistakes
- deployment regressions
- controlled code defects

The same Aegis core must not contain hardcoded assumptions about either workload.

---

# 8. Functional Requirements

## FR-1 — Alert ingestion

Aegis must:

- accept alerts through an authenticated ingestion endpoint
- support idempotent external alert IDs
- deduplicate repeated alerts
- create a durable incident record
- preserve the original alert payload
- associate alerts with services where possible
- preserve source provenance

## FR-2 — Incident timeline

For each incident, Aegis must construct a timeline containing:

- first observed signal
- alert creation
- relevant trace anomalies
- log anomalies
- metric changes
- deployments
- commits
- configuration changes
- agent decisions
- user approvals
- remediation actions
- verification results
- resolution

Timeline entries must include timestamps and source provenance.

## FR-3 — Production topology

Aegis must automatically build and continuously update the production topology.

Nodes may include:

- services
- endpoints
- containers/tasks
- databases
- queues
- external dependencies
- repositories
- deployments
- teams
- owners
- incidents

Relationships may include:

- CALLS
- DEPENDS_ON
- RUNS_AS
- DEPLOYED_BY
- CHANGED_BY
- OWNED_BY
- CAUSED
- AFFECTS
- PRECEDED
- SIMILAR_TO
- REMEDIATED_BY
- VERIFIED_BY

The graph must support point-in-time context where possible.

## FR-4 — Evidence investigation

Aegis must gather evidence from:

- OpenTelemetry telemetry
- Prometheus
- logs
- tracing backend
- infrastructure APIs
- GitHub
- deployment history
- configuration
- incident memory
- runbooks
- knowledge graph

Each evidence item must include:

- source
- timestamp/window
- resource
- evidence type
- retrieval operation
- content or structured value
- trust classification
- provenance identifier

## FR-5 — Hypothesis management

Aegis must maintain multiple candidate hypotheses when evidence does not uniquely identify a cause.

Each hypothesis must contain:

- statement
- confidence/calibration metadata
- supporting evidence
- contradicting evidence
- missing evidence
- affected resources
- expected observable behavior
- next-best investigation action

The system must prefer testing hypotheses over simply selecting one.

## FR-6 — Root-cause analysis

A root cause conclusion must expose:

- primary suspected cause
- confidence
- evidence chain
- causal path
- affected service(s)
- contributing factors
- uncertainty
- rejected alternatives
- missing evidence

Aegis must be able to explicitly return:

**Unknown / unresolved**

when evidence is insufficient.

## FR-7 — Blast-radius analysis

For each active incident, Aegis should estimate:

- directly affected services
- upstream/downstream dependencies
- customer-facing paths
- affected endpoints
- potentially affected resources
- business-impact proxy where configured

Blast radius must be based primarily on graph and telemetry evidence, not an LLM-only guess.

## FR-8 — Change intelligence

Aegis must detect potentially relevant changes:

- source commits
- pull requests
- deployment versions
- container image changes
- configuration changes
- environment changes
- feature flags
- dependency version changes

Changes must be ranked using temporal and topological relevance.

## FR-9 — Code debugging

Aegis must be able to:

1. identify the failing service
2. identify the affected endpoint/path
3. retrieve relevant source code
4. retrieve related tests
5. inspect recent changes
6. reason over stack traces and errors
7. construct a candidate debugging hypothesis
8. propose a patch
9. run static and automated tests
10. reproduce the original failure
11. compare behavior before/after the patch

The system must not represent code as fixed until verification succeeds.

## FR-10 — Patch sandbox

Candidate patches must execute in isolation.

The sandbox must provide:

- repository snapshot
- dependency installation
- deterministic commands where possible
- test execution
- network restrictions
- CPU/memory limits
- wall-clock timeout
- artifact capture
- logs
- exit status
- patch diff

Production credentials must never be present in the patch sandbox.

## FR-11 — Staging verification

Before a code or infrastructure change is promoted to production, Aegis must support staged validation.

Validation may include:

- deployment health
- request success rate
- latency
- error rate
- trace behavior
- targeted regression
- smoke tests
- business workflow tests
- resource utilization
- dependency health

## FR-12 — Risk-based autonomy

Every write action must be categorized:

### Tier 0 — Observe only

No write capability.

### Tier 1 — Low-risk autonomous action

Examples:
- restart a stateless failed task/pod where policy permits
- re-run a safe health check
- re-trigger a known idempotent action
- controlled scale-up within an explicit small bound

Tier 1 requires:
- allowlisted action type
- validated incident evidence
- bounded blast radius
- active policy permission
- action idempotency
- rate limit
- rollback/compensation definition

### Tier 2 — Human approval

Examples:
- rollback deployment
- wider scaling change
- configuration change
- production code promotion

Requires explicit approval.

### Tier 3 — Human-only

Examples:
- destructive database operations
- security-policy changes
- secrets changes
- irreversible migrations
- broad infrastructure modifications

The system must not expose an autonomous execution path for Tier 3.

## FR-13 — Approval workflow

Approval requests must include:

- incident
- proposed action
- reason
- evidence
- confidence
- blast radius
- expected outcome
- risks
- rollback plan
- verification plan
- expiry time

The approval API must enforce authorization server-side.

## FR-14 — Kill switch

Aegis must expose:

- global autonomy kill switch
- per-environment autonomy disable
- per-action-type disable
- per-service disable

The effective policy must fail closed.

## FR-15 — Communication

Aegis should generate:

- engineer-facing incident summaries
- incident-command updates
- stakeholder updates
- resolution summaries
- postmortem drafts

Communication output must distinguish:

- facts
- current hypothesis
- uncertainty
- action status

## FR-16 — Incident memory

On resolution, Aegis should derive a structured memory containing:

- symptoms
- root cause
- evidence pattern
- affected services
- successful fix
- failed attempts
- verification
- prevention action

Only validated/approved memories may become authoritative organizational knowledge.

## FR-17 — Recurring incident detection

Aegis must identify patterns such as:

- same service + same symptom
- same dependency failure
- same deployment pattern
- same alert cluster
- repeated manual remediation

The system should calculate recurrence counts and recommend automation candidates.

## FR-18 — Replay

Any completed incident must be replayable from persisted state without requiring live infrastructure.

Replay must show:

- incident input
- evidence gathered
- tools called
- graph queries
- hypotheses
- decisions
- actions
- evaluations
- approvals
- verification
- final outcome

---

# 9. AI Evaluation Requirements

AI evaluation is not optional.

Every release candidate of Aegis must be evaluated against a versioned benchmark.

## 9.1 Ground-truth incident dataset

Initial goal:

**100+ deterministic incident scenarios**

distributed across:

- SocialNetwork
- Hotel Reservation
- service failure
- latency degradation
- 5xx spike
- timeout
- database degradation
- cache failure
- dependency failure
- resource exhaustion
- bad deployment
- configuration error
- network partition
- cascading failure
- intermittent fault
- false-positive/no-fault alert
- ambiguous incident
- multi-fault incident

Each scenario must have ground truth for:

- incident validity
- root cause
- affected service
- causal dependency path
- expected evidence
- expected blast radius
- severity
- allowed remediation
- correct verification signal

## 9.2 Evaluation metrics

### Detection

- precision
- recall
- F1
- duplicate suppression accuracy

### Service localization

- affected-service exact accuracy
- top-k localization recall

### Root cause

- exact-category accuracy
- semantic root-cause accuracy
- causal-path accuracy
- abstention correctness
- confidence calibration

### Evidence

- citation validity
- evidence precision
- evidence recall
- evidence completeness
- unsupported-claim rate

### Debugging

- faulty-file localization accuracy
- faulty-symbol/function localization accuracy
- patch applicability
- test pass rate
- original-failure reproduction rate
- regression pass rate

### Tool use

- valid tool-call rate
- tool-selection accuracy
- unnecessary tool-call rate
- failed-tool recovery rate
- unsafe-tool attempt rate

### Remediation

- action-selection accuracy
- policy classification accuracy
- false-allow rate
- false-block rate
- successful-remediation rate
- rollback success rate
- verification success rate

### Operational outcomes

- time to first useful hypothesis
- investigation duration
- time to safe remediation
- MTTR
- number of manual interventions
- cost per incident
- token usage
- tool-call count

## 9.3 Statistical requirements

Benchmark results must include uncertainty where practical.

For major quality metrics, report:

- point estimate
- sample count
- bootstrap confidence interval
- scenario breakdown

A change must not be declared an improvement because one aggregate number increased if safety or critical scenario performance regressed.

## 9.4 Release gates

A model/prompt/agent/tool release must pass:

1. no critical safety regression
2. no meaningful increase in unsupported claims
3. no meaningful decrease in root-cause accuracy
4. no increase in unsafe execution attempts
5. acceptable latency/cost budget
6. benchmark reproducibility

---

# 10. AI Observability Requirements

LangSmith is the primary AI observability and evaluation platform.

Aegis must emit traceable runs for:

- incident-level orchestration
- agent invocation
- LLM calls
- tool calls
- MCP calls
- retrieval
- graph queries
- code-analysis tasks
- sandbox execution
- evaluator runs
- remediation decisions

Trace metadata should contain:

- incident ID
- environment
- service
- agent role
- model/provider
- prompt/version identifier
- tool name
- retrieval query
- graph query ID
- latency
- token usage
- estimated cost
- outcome
- evaluator results

Aegis should use OpenTelemetry for product/infrastructure telemetry and LangSmith for agent lifecycle observability. The two are complementary rather than interchangeable.

---

# 11. Non-Functional Requirements

## Reliability

- An unavailable evidence source must degrade into an explicit gap.
- One failed agent task must not corrupt incident state.
- Durable workflow state must survive worker restart.
- Action execution must be idempotent.
- No automatic retries for non-idempotent writes.

## Security

- Infrastructure credentials must never be placed in prompts.
- Agent processes should not receive raw write credentials.
- MCP servers enforce infrastructure access.
- Production writes require explicit policy authorization.
- Secrets must be supplied through environment/secret stores.
- Untrusted logs, tickets, commits, and external content must be treated as data, not instructions.

## Performance targets

Initial engineering targets:

- alert ingestion acknowledgement: <1 s P95
- first useful incident context: <30 s P95
- first evidence-backed hypothesis: <3 min P95
- approval action acknowledgement: <2 s P95
- autonomous safe action decision: <30 s after sufficient evidence
- common replay page load: <2 s P95

Targets are engineering acceptance criteria, not claims about industry norms.

---

# 12. Security and Prompt-Injection Requirements

Operational data can contain attacker-controlled text.

Examples:

- log lines
- HTTP headers
- user input
- commit messages
- issue descriptions
- incident tickets

Therefore:

1. tool outputs must be structurally separated from instructions
2. untrusted text must be marked
3. prompts must define data boundaries
4. tool outputs must pass sanitization/redaction where required
5. agents must not treat evidence text as executable policy
6. action authorization must remain outside the LLM

No prompt can grant permission to perform an action.

Only the policy engine and authenticated execution boundary can grant permission.

---

# 13. Observability of the Observability System

Aegis must detect when its own dependencies are unhealthy.

Examples:

- Prometheus unavailable
- tracing backend unavailable
- Neo4j unavailable
- LangSmith unavailable
- GitHub unavailable
- LLM provider unavailable
- MCP server unavailable

The UI must distinguish:

**No evidence found**

from:

**Evidence source unavailable**

This distinction is operationally critical.

---

# 14. Product UX

Primary surfaces:

## Incident Command Center

Shows:

- incident severity
- current impact
- service topology
- incident timeline
- leading hypotheses
- evidence
- affected services
- recent changes
- recommended next action
- action risk
- verification status

## Investigation Workspace

Provides:

- evidence explorer
- trace explorer
- graph explorer
- source diff view
- hypothesis tree
- agent/tool timeline

## Fix Workspace

Provides:

- diagnosis
- candidate patch
- diff
- tests
- reproduction
- staging result
- regression result
- risk assessment
- promotion/approval controls

## Reliability Overview

Provides:

- recurring incidents
- reliability trends
- noisy alerts
- unresolved root causes
- automation candidates
- remediation success
- AI quality metrics

## AI Evaluation Lab

Provides:

- benchmark suites
- experiments
- model comparisons
- prompt versions
- failure examples
- evaluator scores
- regression history
- cost/latency

---

# 15. Acceptance Criteria for the First Major Release

The first serious release is successful when the following demonstration can be completed end to end:

1. A DeathStarBench application is running with telemetry.
2. A known fault is injected.
3. Aegis receives the incident.
4. Aegis identifies the affected service.
5. Aegis traverses the operational graph.
6. Aegis retrieves correlated metrics, logs, and traces.
7. Aegis checks recent code/deployment changes.
8. Aegis proposes a root cause with grounded evidence.
9. Aegis can identify the relevant code path where applicable.
10. Aegis produces a candidate repair.
11. The repair is tested in isolation.
12. The fault is reproduced.
13. The fix is deployed to staging.
14. Aegis verifies recovery.
15. Risk policy determines whether production approval is needed.
16. A human approves serious changes.
17. Low-risk allowlisted actions may execute autonomously.
18. Production outcome is verified.
19. The incident timeline is complete.
20. The run is available in LangSmith for inspection.
21. The scenario is recorded in the evaluation benchmark.
22. Results can be compared with a prior Aegis version.

---

# 16. Initial Release Sequence

## Phase 1 — Reference environments

- DeathStarBench SocialNetwork
- DeathStarBench Hotel Reservation
- Docker packaging
- Kind deployment
- traffic generation
- fault injection

## Phase 2 — Telemetry foundation

- OpenTelemetry
- Prometheus
- tracing backend
- logs
- unified evidence IDs

## Phase 3 — Knowledge foundation

- Neo4j
- topology ingestion
- Git/deployment relationships
- incident graph
- incident history

## Phase 4 — Investigation agents

- triage
- evidence investigator
- topology investigator
- change investigator
- diagnosis
- verification

## Phase 5 — Debugging

- code retrieval
- sandbox
- reproduction
- patch generation
- automated tests
- staging

## Phase 6 — Safe remediation

- policy engine
- Tier 1
- Tier 2 approval
- Tier 3 blocking
- rollback/compensation
- kill switch

## Phase 7 — Communication and learning

- Slack
- incident summaries
- postmortems
- incident memory
- recurrence detection

## Phase 8 — Evaluation hardening

- 100+ scenarios
- LangSmith datasets
- evaluators
- regression gates
- benchmark reporting

---

# 17. Success Definition

Aegis should ultimately demonstrate:

- faster investigations
- accurate localization
- grounded RCA
- measurable debugging capability
- successful execution-based fixes
- controlled autonomy
- fewer manual steps
- lower MTTR
- low unsafe-action rate
- continuous measurable AI quality

The product's strongest proof is not a beautiful dashboard.

It is a repeatable benchmark in which Aegis resolves known failures faster and more safely than the baseline while making its reasoning and evidence inspectable.

---

# 18. References and Standards

Primary technical references used for this architecture:

- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/specs/semconv/
- LangChain/LangGraph platform documentation: https://docs.langchain.com/
- Neo4j GraphRAG documentation: https://neo4j.com/docs/neo4j-graphrag-python/current/
- AWS container decision guidance: https://docs.aws.amazon.com/decision-guides/latest/decision-guides/choosing-aws-container-service.html
- DeathStarBench: https://github.com/delimitrou/DeathStarBench

