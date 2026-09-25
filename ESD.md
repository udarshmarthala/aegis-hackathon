# ESD.md — Aegis Engineering Specification
## Engineering and System Design Specification

**Status:** New architecture baseline  
**Audience:** Engineers implementing or reviewing Aegis  
**Companion documents:** PRD.md and AIArchitecture.md  
**Primary reference workloads:** DeathStarBench SocialNetwork and Hotel Reservation  
**Local runtime:** Docker + Kind  
**Cloud runtime target:** Amazon ECS/Fargate  
**Agent runtime:** LangGraph  
**AI evaluation and observability:** LangSmith  
**Infrastructure telemetry:** OpenTelemetry  
**Knowledge graph:** Neo4j  
**Transactional state:** PostgreSQL  
**Cache/coordination:** Redis  
**Tool boundary:** MCP

---

# 1. Engineering Objectives

Aegis is engineered as a safety-critical-ish operational automation system.

The architecture therefore prioritizes:

1. deterministic boundaries around AI
2. durable workflow state
3. evidence provenance
4. explicit policy enforcement
5. execution-based verification
6. observable agent behavior
7. reproducible evaluation
8. environment portability

The system should be sophisticated enough to resemble a real SRE product while avoiding infrastructure that exists only for architectural fashion.

---

# 2. Architectural Tenets

## 2.1 LLMs are probabilistic components

LLMs may:

- classify
- summarize
- hypothesize
- plan investigations
- select tools
- analyze code
- generate patches

LLMs must not be the final authority for:

- authentication
- authorization
- policy
- risk tier
- action permission
- idempotency
- resource locking
- rollback state
- evaluation pass/fail

## 2.2 Deterministic systems guard consequential actions

The action path is:

```text
Agent proposal
    ↓
Schema validation
    ↓
Evidence validation
    ↓
Policy/risk engine
    ↓
Authorization
    ↓
Resource lease / concurrency guard
    ↓
Execution
    ↓
Verification
    ↓
Commit / rollback / escalation
```

The agent cannot bypass a gate by changing its response text.

---

# 3. High-Level Architecture

```text
                         ┌──────────────────────────────┐
                         │       External Signals       │
                         │ Alerts / OTel / Deployments  │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────┐
                         │       Aegis API Layer        │
                         │ Ingestion / UI / Approvals   │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────┐
                         │     Durable Agent Runtime    │
                         │          LangGraph            │
                         └──────────────┬───────────────┘
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 │                      │                      │
                 ▼                      ▼                      ▼
          Investigation           Debugging             Communication
                 │                      │
                 ▼                      ▼
          MCP Tool Layer         Sandbox/Code Runtime
                 │                      │
       ┌─────────┼─────────┐            │
       ▼         ▼         ▼            ▼
      K8s       ECS      OTel/Git     Git repository
       │         │         │            │
       └─────────┼─────────┘            │
                 │                      │
                 └──────────┬───────────┘
                            ▼
                    Knowledge Plane
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           Neo4j        Postgres       Vector
           Graph         State        Retrieval
              │            │            │
              └────────────┼────────────┘
                           ▼
                    Decision / Policy
                           │
                           ▼
                    Action Executor
                      │         │
                   Staging   Production
                     │          │
                     └────┬─────┘
                          ▼
                     Verification

AI observability/evaluation:
all agent/LLM/retrieval/tool/evaluation runs → LangSmith

Infrastructure/application observability:
application + platform → OpenTelemetry
```

---

# 4. Deployment Environments

## 4.1 Local development

```text
Windows/Linux/macOS
    ↓
Docker
    ↓
Kind
    ↓
DeathStarBench workloads
```

Infrastructure services:

- PostgreSQL
- Redis
- Neo4j
- OTel Collector
- Prometheus
- tracing backend
- log backend
- Aegis API
- Aegis agent workers
- Next.js frontend
- fault-injection/traffic harness

The entire environment should be reproducible through Compose + Kind setup scripts.

## 4.2 Cloud target

Primary cloud application runtime:

```text
Route 53 / ALB
      ↓
ECS/Fargate
      ├── Aegis API
      ├── Aegis worker
      ├── Aegis frontend
      └── application services
```

Supporting AWS services may include:

- ECR
- RDS PostgreSQL
- ElastiCache Redis
- S3
- CloudWatch
- Secrets Manager
- IAM
- SES/SNS where communication requirements justify them

ECS/Fargate is chosen because the product's core contract is container-centric and Aegis should not require Kubernetes merely to run. AWS documents ECS as a managed container orchestration service and Fargate as serverless compute for containers. Kubernetes remains an adapter and an important test target rather than the product's only execution environment.

---

# 5. Application Adapter Model

Aegis must never contain DeathStarBench-specific reasoning in the core.

Define:

```python
class EnvironmentAdapter(Protocol):
    async def list_services(...)
    async def get_service(...)
    async def get_deployment(...)
    async def get_instance_state(...)
    async def get_recent_changes(...)
    async def execute_action(...)
```

Implementations:

```text
KubernetesAdapter
ECSAdapter
```

An adapter normalizes source-specific objects into Aegis domain entities.

Example normalized service:

```json
{
  "service_id": "socialnetwork:compose-post-service",
  "environment": "kind-local",
  "runtime": "kubernetes",
  "version": "sha-abc123",
  "desired_instances": 2,
  "ready_instances": 1
}
```

ECS equivalents must expose the same domain contract.

---

# 6. Data Architecture

Aegis intentionally uses multiple stores because they solve different problems.

## PostgreSQL

System of record for:

- incidents
- users
- approvals
- action records
- audit events
- workflow metadata
- evaluation metadata
- benchmark results
- immutable evidence references
- configuration
- policy versions

PostgreSQL must not be treated as the graph database.

## Neo4j

Operational knowledge graph containing entities and relationships required for topology and causal navigation.

Examples:

```text
(:Service)-[:CALLS]->(:Service)
(:Service)-[:DEPENDS_ON]->(:Database)
(:Service)-[:DEPLOYED_AS]->(:Deployment)
(:Deployment)-[:CREATED_BY]->(:Commit)
(:Commit)-[:CHANGED]->(:CodeSymbol)
(:Service)-[:OWNED_BY]->(:Team)
(:Incident)-[:AFFECTED]->(:Service)
(:Incident)-[:SIMILAR_TO]->(:Incident)
(:Remediation)-[:VERIFIED_BY]->(:Verification)
```

Neo4j is preferred here because the product's operational problem is strongly graph-shaped. The graph should support traversal and relationship reasoning, while PostgreSQL remains the source of truth for transactional state.

## Vector retrieval

Vector retrieval stores semantic representations for:

- runbooks
- postmortems
- incident summaries
- code documentation
- selected evidence summaries

Graph retrieval and vector retrieval are complementary.

Example:

```text
Question:
"Have we previously seen checkout latency after a cache deployment?"

Vector:
find semantically similar incidents

Graph:
check same service + dependency + deployment relationship

Combined:
rank historical incidents with structural relevance
```

---

# 7. Knowledge Graph Ingestion

Graph ingestion sources:

- runtime discovery
- OpenTelemetry service relationships
- deployment metadata
- GitHub repository metadata
- repository source structure
- incidents
- ownership information
- runbooks
- remediation events

The ingestion pipeline should be incremental.

```text
source event
   ↓
normalizer
   ↓
entity resolver
   ↓
relationship resolver
   ↓
Neo4j upsert
```

Entity identity must be stable.

Example:

A Kubernetes pod restart must not create a new service entity.

Use canonical IDs at the service/deployment/repository/resource level.

---

# 8. Operational Evidence Model

Define a common `EvidenceItem`.

Suggested fields:

```text
id
incident_id
source
source_type
retrieved_at
observed_at
resource_id
evidence_type
content
structured_value
provenance_uri
trust_class
redaction_state
hash
```

Examples:

```text
metric:
  p99 latency increased from 0.18s to 2.4s

trace:
  checkout → payment → timeout

log:
  connection pool exhausted

deployment:
  image changed from v1.8 to v1.9

git:
  commit changed payment timeout configuration
```

Every evidence item must remain independently inspectable.

---

# 9. Agent Runtime

Use LangGraph as the durable orchestration runtime.

LangGraph is used for:

- persistent workflow state
- bounded branching
- conditional routing
- human interrupts
- resume
- checkpointing
- streaming progress
- stateful multi-step workflows

Aegis should not build a permanently linear chain.

Instead:

```text
Incident State
      ↓
Triage
      ↓
Investigation Planner
      ↓
Parallel Investigation
 ┌────┼────┬────┐
 ↓    ↓    ↓    ↓
OTel  Graph Git  Runtime
 └────┼────┴────┘
      ↓
Evidence synthesis
      ↓
Hypothesis generation
      ↓
Hypothesis test
      ├── more evidence
      └── sufficient evidence
               ↓
             Debug
               ↓
          Candidate repair
               ↓
            Verify
               ↓
          Risk / Policy
               ↓
        Human or Autonomous
```

Loops are bounded by explicit counters/time budgets.

---

# 10. Agent Organization

Do not create a large cast of always-on agents.

Use role-based capability agents.

## Supervisor / Incident Orchestrator

Responsibilities:

- maintain workflow state
- route work
- enforce budgets
- determine when to stop
- trigger human escalation

It should not directly mutate production infrastructure.

## Investigator

Responsibilities:

- formulate investigation objectives
- retrieve relevant telemetry
- request targeted tools
- maintain evidence coverage

## Topology Analyst

Responsibilities:

- graph traversal
- dependency analysis
- blast radius
- service relationship reasoning

## Change Analyst

Responsibilities:

- deployment analysis
- Git history
- configuration changes
- change-impact reasoning

## Diagnosis Agent

Responsibilities:

- compare hypotheses
- weigh evidence
- produce grounded causal explanation
- abstain when needed

## Debugger

Responsibilities:

- inspect source
- locate likely defect
- create candidate patch
- design reproduction
- request sandbox execution

## Verifier

Responsibilities:

- validate evidence
- challenge diagnosis
- validate test results
- determine whether expected recovery occurred

## Remediation Planner

Responsibilities:

- formulate action proposal
- estimate intended outcome
- identify rollback

## Communication Agent

Responsibilities:

- produce audience-specific updates
- separate facts from hypotheses
- avoid exposing operational noise unnecessarily

---

# 11. Agent State

The state object should be typed and explicit.

Illustrative shape:

```python
class IncidentState(TypedDict):
    incident_id: str
    phase: str
    severity: str
    environment: str

    affected_services: list[str]

    evidence: list[EvidenceRef]
    hypotheses: list[Hypothesis]

    selected_hypothesis_id: str | None
    confidence: float | None

    graph_context: GraphContext
    change_context: ChangeContext

    code_targets: list[CodeTarget]
    candidate_repairs: list[RepairCandidate]

    verification_results: list[VerificationResult]

    proposed_action: ActionProposal | None
    policy_decision: PolicyDecision | None

    human_approval: ApprovalState | None

    budget: BudgetState
    errors: list[WorkflowError]
```

Large evidence bodies should not be copied into every graph state transition. Persist evidence separately and reference it.

---

# 12. MCP Architecture

MCP is the standard boundary through which agents access external capabilities.

Initial servers/capabilities:

```text
otel-mcp
metrics-mcp
logs-mcp
traces-mcp
kubernetes-mcp
ecs-mcp
github-mcp
neo4j-mcp
database-readonly-mcp
deployment-mcp
communication-mcp
```

A single gateway may multiplex servers, but logical authorization must remain per server/tool.

Every tool must declare:

- input schema
- output schema
- read/write classification
- risk class
- timeout
- retry policy
- idempotency behavior
- whether output contains untrusted text

---

# 13. Tool Safety

Tool execution rules:

### Read-only tools

May be called automatically within investigation budgets.

### Read/write tools

Require policy classification before invocation.

### Destructive tools

Must be structurally unavailable to autonomous agents unless the platform explicitly enables a guarded human-controlled path.

The MCP boundary is not itself the authorization policy. Aegis policy must decide whether a tool call is permitted.

---

# 14. Code Intelligence

Repository ingestion should create relationships between:

- repository
- branch
- commit
- file
- module
- class
- function
- test
- deployment artifact
- service

The initial implementation may use source parsing plus Git metadata.

Do not send entire repositories into an LLM.

Use hierarchical retrieval:

```text
incident symptom
   ↓
service
   ↓
endpoint
   ↓
repository
   ↓
changed files
   ↓
symbols/functions
   ↓
relevant tests
```

Only then build a bounded code context.

---

# 15. Debug Sandbox

Aegis needs a sandbox because code generation without execution is insufficient.

The sandbox receives:

- repository commit SHA
- candidate patch
- dependency lockfiles
- test command
- reproduction command

The sandbox returns:

```text
build_status
test_status
failing_tests_before
failing_tests_after
regression_tests
reproduction_status
stdout
stderr
artifacts
duration
resource_usage
```

The sandbox has:

- no production credentials
- no unrestricted network
- fixed CPU/memory
- wall-clock limit
- ephemeral filesystem
- unique execution ID

---

# 16. Fault Injection Harness

The evaluation environment requires deterministic incident creation.

A scenario definition should contain:

```yaml
id: SN-CACHE-001
workload: socialnetwork
fault:
  target: redis-service
  mode: latency
  magnitude: 800ms
trigger:
  type: traffic_threshold
ground_truth:
  root_cause: redis_latency
  affected_services:
    - home-timeline-service
  severity: P2
allowed_action:
  tier: 1
verification:
  metric: request_latency_p99
  recovery_threshold: ...
```

Fault injection must be separated from Aegis itself.

This prevents the benchmark from accidentally giving the agent privileged knowledge about the injected fault.

---

# 17. Verification Engine

Verification is a deterministic subsystem.

It should compare:

### Before

- error rate
- latency
- throughput
- saturation
- affected requests
- trace failures

### After

- same metrics
- same endpoints
- same customer path
- resource health
- regression tests

A repair passes only when the expected signal changes in the expected direction and no protected metrics regress.

---

# 18. Risk / Policy Engine

Policy input:

```text
incident severity
service criticality
action type
target resource
blast radius
evidence quality
diagnosis confidence
verification state
rollback availability
change scope
time window
environment
current autonomy mode
```

Output:

```text
ALLOW
REQUIRE_HUMAN
BLOCK
```

Risk tier and policy decision must be recorded separately.

This prevents an LLM from "talking" an unsafe action into a lower risk class.

---

# 19. Concurrency and Idempotency

All write actions need:

- unique action ID
- idempotency key
- resource lease where applicable
- expiry
- execution state
- verification state

The system must prevent two agents from simultaneously changing the same resource.

Suggested database pattern:

```text
resource_leases
unique active lease(resource_type, resource_id)
```

The database is the concurrency arbiter.

---

# 20. Action State Machine

```text
PROPOSED
   ↓
POLICY_CHECKED
   ↓
┌───────────────┬─────────────────┐
↓               ↓                 ↓
BLOCKED      HUMAN_REQUIRED      ALLOWED
                 ↓                 ↓
              APPROVED         EXECUTING
                 ↓                 ↓
               EXECUTING       VERIFYING
                   └──────┬────────┘
                          ↓
                 ┌────────┼────────┐
                 ↓        ↓        ↓
              SUCCESS   FAILED   ROLLBACK
```

A stale approval cannot be executed.

---

# 21. Incident State Machine

Suggested state model:

```text
RECEIVED
TRIAGING
INVESTIGATING
DIAGNOSING
DEBUGGING
VERIFYING
AWAITING_APPROVAL
REMEDIATING
MONITORING
RESOLVED
ESCALATED
BLOCKED
```

State transitions must be persisted.

---

# 22. Database Schema

Core PostgreSQL tables:

```text
users
incidents
incident_alerts
incident_state_transitions
evidence_items
hypotheses
hypothesis_tests
agent_runs
agent_messages
tool_calls
approvals
policy_decisions
remediation_actions
resource_leases
verification_runs
incident_memories
runbooks
evaluation_runs
benchmark_cases
benchmark_results
audit_log
```

Use JSONB for evolving metadata while keeping core query dimensions normalized.

---

# 23. Neo4j Schema

Example node labels:

```text
Service
Endpoint
Deployment
Container
Task
Pod
Repository
Commit
File
CodeSymbol
Database
Queue
ExternalDependency
Team
Incident
Alert
Runbook
Remediation
Verification
```

Example relationships:

```text
(:Service)-[:CALLS]->(:Service)
(:Service)-[:EXPOSES]->(:Endpoint)
(:Service)-[:USES]->(:Database)
(:Service)-[:DEPLOYED_AS]->(:Deployment)
(:Deployment)-[:CREATED_BY]->(:Commit)
(:Commit)-[:MODIFIES]->(:File)
(:File)-[:DEFINES]->(:CodeSymbol)
(:Service)-[:OWNED_BY]->(:Team)
(:Incident)-[:AFFECTS]->(:Service)
(:Incident)-[:SUSPECTS]->(:CodeSymbol)
(:Incident)-[:SIMILAR_TO]->(:Incident)
(:Remediation)-[:TARGETS]->(:Service)
(:Remediation)-[:VERIFIED_BY]->(:Verification)
```

---

# 24. GraphRAG

GraphRAG should be used where relationship structure matters.

Use vector retrieval for semantic matching.

Use graph traversal for:

- dependency chains
- ownership
- change lineage
- incident similarity by structure
- blast radius
- service topology
- code/deployment lineage

The answer should combine:

```text
semantic relevance
+
structural relevance
+
temporal relevance
+
operational evidence
```

Aegis must not use GraphRAG as a replacement for direct observability queries.

---

# 25. Redis

Redis is used for:

- cache
- short-lived coordination
- rate limits
- event fan-out where useful
- transient investigation hints

Redis is never the authoritative store for:

- incident state
- approvals
- action history
- audit trail
- evaluation truth

---

# 26. Async Execution

The FastAPI process must not perform long-running investigations synchronously.

Flow:

```text
API
 ↓
persist incident
 ↓
enqueue workflow
 ↓
worker
 ↓
LangGraph run
```

Use durable LangGraph checkpoints.

A small worker service is preferred over introducing another workflow platform unless later scale requirements prove the need.

Do not add Temporal merely because it is sophisticated; LangGraph durable execution plus a disciplined action layer is sufficient for the first production-grade version.

---

# 27. Frontend

Stack:

- Next.js
- TypeScript
- Tailwind
- component system
- SSE or WebSocket stream for active incidents
- server-side authorization awareness
- progressive data loading

Main pages:

```text
/
dashboard
incidents/[id]
incidents/[id]/graph
incidents/[id]/debug
approvals
reliability
evaluation
settings
```

The UI should present uncertainty clearly.

Do not compress:

- evidence
- hypothesis
- decision
- action

into one AI-generated paragraph.

---

# 28. AI Observability

LangSmith must capture:

```text
trace
 ├── incident workflow
 │    ├── investigator
 │    │    ├── LLM
 │    │    ├── MCP tool
 │    │    └── retrieval
 │    ├── diagnosis
 │    ├── debugger
 │    ├── verifier
 │    └── policy decision
```

Every run should carry stable tags:

```text
incident_id
scenario_id
environment
workload
model
agent_version
prompt_version
policy_version
graph_version
retriever_version
```

This enables exact comparisons between experiments.

---

# 29. LangSmith Evaluation

Create datasets for:

1. incident triage
2. evidence grounding
3. root cause
4. topology localization
5. code debugging
6. patch generation
7. action planning
8. safety decisions
9. communication
10. end-to-end incidents

Each benchmark experiment must persist:

- dataset version
- model version
- prompt version
- agent build/version
- evaluator versions
- result
- failure cases
- latency
- token/cost data

---

# 30. Evaluator Architecture

Use deterministic evaluators whenever the ground truth allows it.

Examples:

```text
Root cause:
exact/normalized match

Affected service:
exact match

Citation:
citation ID exists + source evidence matches

Patch:
tests + reproduction

Safety:
policy oracle

Remediation:
environment state before/after

Latency:
measured wall time

Cost:
actual model usage
```

Use LLM-as-judge only for dimensions where deterministic evaluation is genuinely insufficient, such as qualitative explanation quality.

All judge-based metrics must be versioned and calibrated.

---

# 31. Baselines

The benchmark should include multiple baselines:

### Baseline A — manual-like deterministic workflow

Fixed tool search without AI reasoning.

### Baseline B — single-agent baseline

One agent receives all available tools.

### Baseline C — Aegis

Full architecture.

### Ablations

- no graph
- no retrieval
- no evidence verifier
- no change intelligence
- no hypothesis loop
- no execution verification

The purpose is to determine which architectural pieces actually produce value.

---

# 32. Evaluation Failure Taxonomy

Every failed scenario should be classified.

```text
DETECTION_FAILURE
LOCALIZATION_FAILURE
EVIDENCE_FAILURE
GROUNDING_FAILURE
CAUSALITY_FAILURE
TOOL_SELECTION_FAILURE
RETRIEVAL_FAILURE
CODE_LOCALIZATION_FAILURE
PATCH_FAILURE
VERIFICATION_FAILURE
POLICY_FAILURE
EXECUTION_FAILURE
OBSERVABILITY_FAILURE
TIMEOUT
PROVIDER_FAILURE
ENVIRONMENT_FAILURE
```

Environment/harness failures must never be counted as model-quality failures.

---

# 33. Cost and Latency Budgets

Every workflow receives a budget:

```text
wall clock
LLM calls
tokens
tool calls
graph queries
sandbox runtime
```

When budget is exhausted:

- stop investigation
- preserve state
- produce best-supported conclusion
- escalate

An agent may not increase its own budget.

---

# 34. Failure Handling

MCP failure:

```text
retry bounded
    ↓
unavailable
    ↓
record evidence gap
    ↓
continue when safe
```

LLM failure:

```text
provider fallback
    ↓
same schema
    ↓
same policy
```

Provider fallback must not weaken safety.

Neo4j failure:

```text
graph unavailable
    ↓
mark topology evidence incomplete
    ↓
use direct telemetry if sufficient
    ↓
reduce confidence if necessary
```

LangSmith failure must never halt the incident workflow.

Observability backends are not control-plane dependencies.

---

# 35. Security Boundary

Credentials belong to integration boundaries.

Bad:

```text
LLM agent → AWS credentials
```

Good:

```text
LLM
  ↓
MCP tool request
  ↓
policy
  ↓
MCP server
  ↓
scoped IAM/service credentials
  ↓
AWS API
```

The agent only knows:

> "The action succeeded/failed and this is the result."

It does not receive secret material.

---

# 36. Infrastructure Implementations

## Kubernetes MCP

Read operations:

- services
- pods
- deployments
- events
- logs
- resource state
- namespaces
- workload metadata

Write operations only through explicitly allowlisted tools.

## ECS MCP

Read operations:

- clusters
- services
- tasks
- task definitions
- deployments
- target health
- CloudWatch log references
- task metadata

Write operations:

- controlled service scaling
- task replacement
- deployment actions according to policy

## GitHub MCP

Read:

- commits
- pull requests
- diffs
- files
- blame/history where required

Write path should not directly merge to production.

Aegis should create proposed changes and allow normal CI/CD controls to remain part of the promotion process.

---

# 37. CI/CD Integration

Recommended code-fix promotion flow:

```text
Aegis patch
   ↓
isolated branch/worktree
   ↓
tests
   ↓
security/static checks
   ↓
CI
   ↓
container image
   ↓
staging
   ↓
fault reproduction
   ↓
verification
   ↓
policy
   ↓
human approval if required
   ↓
production
```

Aegis should integrate with existing deployment pipelines rather than becoming a second hidden deployment system.

---

# 38. Staging Strategy

Staging must be as close as practical to production.

For the benchmark environment:

```text
faulted workload
     ↓
diagnosis
     ↓
patch
     ↓
clean staging environment
     ↓
deploy candidate
     ↓
replay same traffic/failure
     ↓
compare telemetry
```

A patch that merely makes the original metric look better while breaking another workflow must fail verification.

---

# 39. Rollback

Every executable action must expose one of:

- inverse action
- safe compensating action
- pipeline rollback
- immutable deployment rollback

If there is no known safe rollback path, the action should generally require human approval or be blocked.

---

# 40. Audit Requirements

Audit every:

- user action
- agent action
- tool call
- evidence access
- hypothesis change
- policy decision
- approval
- execution
- rollback
- verification
- evaluator result

Audit entries should include a correlation ID that links them to the incident trace and LangSmith run.

---

# 41. Proposed Repository Structure

```text
aegis/
├── apps/
│   ├── api/
│   ├── worker/
│   └── web/
├── packages/
│   ├── domain/
│   ├── agent_runtime/
│   ├── policy/
│   ├── evidence/
│   ├── graph/
│   ├── retrieval/
│   ├── execution/
│   ├── verification/
│   └── evaluation/
├── mcp/
│   ├── otel/
│   ├── kubernetes/
│   ├── ecs/
│   ├── github/
│   ├── neo4j/
│   └── communication/
├── environments/
│   ├── deathstar-socialnetwork/
│   ├── deathstar-hotelreservation/
│   ├── kind/
│   └── ecs/
├── eval/
│   ├── datasets/
│   ├── scenarios/
│   ├── evaluators/
│   ├── baselines/
│   └── reports/
├── infra/
│   ├── docker/
│   ├── terraform/
│   └── ci/
└── docs/
```

The exact migration from the current repository should happen incrementally. Do not perform a wholesale rename if it creates unnecessary risk; preserve working modules until their replacement has test coverage.

---

# 42. Migration From Existing Aegis

The current Aegis repository already contains valuable foundations:

- FastAPI
- LangGraph-style orchestration
- MCP gateway
- agent modules
- Postgres state
- Redis
- RAG
- redaction
- safety controls
- approvals
- resource leases
- circuit breakers
- kill switch
- evaluation scripts
- scenario validation

The redesign should reuse these mechanisms.

Major changes:

1. replace explicit three-service Meridian topology with dynamic topology
2. add DeathStarBench SocialNetwork
3. add DeathStarBench Hotel Reservation
4. make Neo4j the graph store
5. make OpenTelemetry the primary telemetry contract
6. add ECS adapter
7. extend debugging into code/sandbox/staging
8. make LangSmith the evaluation and AI observability center
9. add formal benchmark/evaluator architecture
10. replace fixed agent sequencing with bounded dynamic investigation

The migration should happen behind stable domain interfaces.

---

# 43. Testing Strategy

## Unit

- schemas
- state transitions
- policy
- risk classification
- evidence validation
- graph mapping
- retriever
- parsers

## Integration

- MCP servers
- Neo4j
- Postgres
- OpenTelemetry
- GitHub
- sandbox
- LangSmith callbacks

## End-to-end

Each deterministic fault scenario should be reproducible from scratch.

## Chaos

Inject failure into the Aegis dependencies themselves:

- kill MCP server
- make Neo4j unavailable
- throttle LLM provider
- corrupt a retrieval source
- interrupt worker
- terminate sandbox

The expected behavior must be safe degradation rather than fabricated certainty or unsafe action.

---

# 44. Engineering Definition of Done

A feature is not done when the happy-path demo works.

A feature is done when:

- typed interfaces exist
- failure behavior is defined
- permissions are enforced
- metrics are emitted
- traces are visible
- evaluation coverage exists
- tests pass
- replay/audit behavior works
- unsafe paths are impossible or blocked

---

# 45. References

- LangChain/LangGraph documentation: https://docs.langchain.com/
- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/specs/semconv/
- Neo4j GraphRAG Python documentation: https://neo4j.com/docs/neo4j-graphrag-python/current/
- AWS container service decision guide: https://docs.aws.amazon.com/decision-guides/latest/decision-guides/choosing-aws-container-service.html
- DeathStarBench: https://github.com/delimitrou/DeathStarBench
