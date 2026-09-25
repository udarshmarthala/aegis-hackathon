# AIArchitecture.md — Aegis AI Architecture
## Agentic Investigation, Debugging, Evaluation, and Autonomous Operations

**Status:** New architecture baseline  
**Purpose:** Define how AI is used inside Aegis, how agents interact with tools and knowledge, how agent behavior is controlled, and how AI quality is measured.  
**Runtime:** LangGraph  
**AI observability/evaluation:** LangSmith  
**Tool protocol:** MCP  
**Knowledge graph:** Neo4j  
**Infrastructure telemetry:** OpenTelemetry  
**Transactional state:** PostgreSQL

---

# 1. Architectural Thesis

The central design decision is:

> **Aegis is an evidence-driven agentic control system, not an LLM-driven automation script.**

The LLM supplies reasoning and planning where probabilistic intelligence is useful.

Deterministic components own:

- evidence identity
- schemas
- graph integrity
- permissions
- policy
- action safety
- concurrency
- idempotency
- execution
- verification
- benchmark truth

This separation is essential because a fluent explanation is not the same thing as a correct diagnosis.

---

# 2. AI System Goals

Aegis AI should be capable of:

1. understanding incidents
2. selecting useful evidence sources
3. reasoning across telemetry
4. traversing system topology
5. connecting incidents to code changes
6. forming competing hypotheses
7. testing hypotheses
8. debugging source code
9. generating candidate patches
10. validating changes through execution
11. choosing safe remediation
12. communicating status
13. learning from prior incidents
14. abstaining when evidence is insufficient

---

# 3. What Changed From the Original Agent Architecture

The old conceptual architecture was broadly:

```text
Triage
  ↓
Correlation
  ↓
RCA
  ↓
Observer
  ↓
Resolution
  ↓
Communication
```

That is useful as a conceptual decomposition, but it is too rigid for a modern agentic engineering system.

The new model is:

```text
              Incident Orchestrator
                       │
          ┌────────────┼────────────┐
          ↓            ↓            ↓
     Evidence       Graph        Change
     Investigator   Analyst      Analyst
          └────────────┼────────────┘
                       ↓
                  Hypothesis
                    Engine
                       │
                ┌──────┴──────┐
                ↓             ↓
             Verify        Investigate More
                │
                ↓
              Debugger
                │
        ┌───────┴────────┐
        ↓                ↓
   Patch candidate   No safe patch
        ↓
     Sandbox
        ↓
     Staging
        ↓
   Verification
        ↓
    Risk Policy
        ↓
   Action / Human
```

Agents are therefore **capabilities invoked by a durable orchestrator**, not a fixed cast of personalities that always run in the same sequence.

---

# 4. Why LangGraph

LangGraph is used as the orchestration runtime because Aegis requires:

- durable execution
- resumable workflows
- explicit state
- branching
- loops
- human interrupts
- checkpoints
- streaming
- bounded long-running tasks

LangChain's current platform documentation positions LangGraph as the orchestration runtime for durable execution, streaming, human-in-the-loop, and persistence, while LangSmith covers tracing, evaluation, deployment, and monitoring.

Aegis should use LangGraph's control-flow strengths instead of hiding the entire product inside a generic ReAct loop.

---

# 5. Dynamic Investigation Model

An incident investigation is a partially observable problem.

The agent does not know the root cause initially.

Instead:

```text
Observation
   ↓
Hypotheses
   ↓
Information gain
   ↓
Next investigation action
   ↓
New observation
   ↓
Hypothesis update
```

The next tool call should be selected based on:

- current evidence
- strongest uncertainty
- topology
- hypothesis disagreement
- expected information gain
- cost
- latency
- remaining budget

The agent must not repeatedly call the same tools simply because they are available.

---

# 6. Incident State as the Agent's Working Memory

The state should be structured.

```text
IncidentContext
├── identity
├── severity
├── environment
├── services
├── topology
├── timeline
├── evidence references
├── hypotheses
├── tests performed
├── code context
├── repairs
├── verification
├── policy
└── budget
```

Large raw evidence should be stored separately.

Agent state contains references and summaries, not unlimited logs.

---

# 7. Hierarchical Intelligence

Aegis uses three logical levels.

## Level 1 — Deterministic control

Responsibilities:

- workflow transitions
- budgets
- schemas
- permissions
- state
- leases
- policy
- execution

## Level 2 — Specialized agents

Responsibilities:

- investigation
- topology
- code
- diagnosis
- verification
- communication

## Level 3 — LLM calls

Responsibilities:

- semantic interpretation
- hypothesis generation
- code reasoning
- summarization
- planning

This prevents low-level model behavior from becoming hidden control flow.

---

# 8. Agent Roles

## 8.1 Incident Orchestrator

The orchestrator is responsible for:

- selecting the next capability
- maintaining state
- checking budgets
- creating investigation tasks
- handling failures
- deciding when sufficient evidence exists
- pausing for human action

It should be as deterministic as possible.

Use model assistance only for ambiguous routing.

## 8.2 Evidence Investigator

The Evidence Investigator answers:

> "What information do we still need?"

It can use:

- metrics
- logs
- traces
- alerts
- deployment metadata
- infrastructure state

It should return structured evidence requests, not arbitrary tool calls.

## 8.3 Topology Analyst

The Topology Analyst uses Neo4j and runtime evidence to answer:

- What calls what?
- Where did failure propagate?
- Which service is a plausible origin?
- What is the blast radius?

It should prefer graph queries over LLM memory.

## 8.4 Change Analyst

Checks:

- recent commits
- deployments
- configuration
- feature flags
- dependencies

It should correlate change timing and dependency path.

A recent commit is not a cause merely because it is recent.

## 8.5 Diagnosis Agent

The Diagnosis Agent integrates evidence.

It returns:

```json
{
  "hypotheses": [
    {
      "id": "H1",
      "statement": "...",
      "supporting_evidence": ["E12", "E18"],
      "contradicting_evidence": ["E31"],
      "confidence": 0.78
    }
  ],
  "recommended_test": "...",
  "abstain": false
}
```

A diagnosis without evidence references is invalid.

## 8.6 Debugger

The Debugger bridges operations and software engineering.

Responsibilities:

- identify code path
- inspect symbols
- inspect recent diffs
- infer failure mechanism
- formulate reproduction
- propose patch

## 8.7 Verifier

The Verifier is intentionally adversarial.

It asks:

- Does the evidence actually support the diagnosis?
- Does the patch address the identified failure?
- Did the original failure reproduce?
- Did the fix stop the failure?
- Did another metric get worse?
- Is the evidence source healthy?

The Verifier must be able to reject the main agent's conclusion.

## 8.8 Remediation Planner

Creates a structured action proposal:

```text
action_type
target
reason
expected_effect
risk
blast_radius
rollback
verification
```

The planner does not authorize itself.

## 8.9 Communication Agent

Generates audience-specific views of the same underlying state.

Engineering message:

> payment dependency timeouts increased after deployment 8f3a...

Executive message:

> Checkout transactions are degraded. The team has isolated the issue to a payment dependency and is validating a rollback.

Both must come from the same structured incident truth.

---

# 9. Multi-Agent Coordination

Do not force agents to communicate through free-form natural-language chat.

Preferred pattern:

```text
Agent
  ↓
typed artifact
  ↓
shared state / database
  ↓
next agent
```

Artifacts include:

- `EvidenceBundle`
- `HypothesisSet`
- `TopologyAssessment`
- `ChangeAssessment`
- `CodeAssessment`
- `RepairCandidate`
- `VerificationResult`
- `ActionProposal`

This reduces ambiguity and makes evaluation easier.

---

# 10. Parallel Agent Work

When independent evidence sources exist, run them in parallel.

Example:

```text
             Investigation
             /    |      \
            /     |       \
        Traces   Metrics   Git
            \     |       /
             \    |      /
             Evidence Merge
```

The orchestrator should define bounded parallelism.

Do not spawn unlimited subagents.

---

# 11. Subagent Creation

Aegis may create temporary specialized tasks.

Example:

```text
"Investigate whether payment-service is the origin"
```

A temporary investigator may:

- query traces
- inspect metrics
- inspect relevant logs
- inspect dependency relationships

It returns a structured finding.

Temporary agents should have:

- explicit goal
- tool allowlist
- call budget
- time budget
- output schema
- parent incident ID

---

# 12. Tool Selection

A tool is selected based on information need.

Examples:

### Need to confirm latency

Use metrics/traces.

### Need to confirm a deployment regression

Use deployment metadata + Git diff.

### Need to confirm dependency propagation

Use traces + Neo4j.

### Need to locate source defect

Use code retrieval + Git.

### Need to determine whether a fix worked

Use verification queries and tests.

The LLM should not be expected to memorize which API endpoint corresponds to every operation. Tool schemas should make available operations explicit.

---

# 13. Evidence Hierarchy

Not all evidence is equal.

Suggested trust hierarchy:

## Tier A — Direct machine observations

- metric values
- trace spans
- task status
- exit codes
- test results

## Tier B — Structured metadata

- deployment record
- Git commit metadata
- graph relationships
- configuration version

## Tier C — Human-authored context

- runbooks
- incident notes
- postmortems

## Tier D — Untrusted free text

- user input
- log message content
- arbitrary commit messages
- external text

The LLM may consume all four, but the platform must preserve this distinction.

---

# 14. Grounded Reasoning

Aegis should enforce a reasoning contract:

```text
Claim
  ↓
Evidence IDs
  ↓
Evidence validation
  ↓
Claim allowed
```

Example invalid claim:

> "The deployment caused the outage."

when no deployment evidence exists.

Correct behavior:

> "A recent deployment is temporally correlated, but current evidence is insufficient to establish it as the cause."

---

# 15. Hypothesis Testing

Each hypothesis should define expected observations.

Example:

```text
Hypothesis:
Redis latency is causing checkout timeout.

Predictions:
- Redis p99 increases
- downstream timeout spans increase
- checkout latency follows Redis latency
- error rate increases on dependent endpoints
```

The agent should query for these predictions.

This moves Aegis from narrative RCA toward falsifiable diagnosis.

---

# 16. Confidence and Calibration

Confidence must not simply copy an LLM's self-reported number.

Aegis should derive a structured confidence signal from:

- evidence coverage
- evidence contradiction
- independent corroboration
- source reliability
- hypothesis test results
- historical benchmark performance

The final confidence model can be calibrated using the benchmark.

Recommended measurements:

- Brier score
- expected calibration error
- reliability diagrams
- selective accuracy at different confidence thresholds

Aegis should optimize not only for correctness but for knowing when it is uncertain.

---

# 17. Retrieval Architecture

Use hybrid retrieval:

```text
Incident Query
      │
      ├── lexical retrieval
      ├── vector retrieval
      ├── graph traversal
      └── metadata filtering
               ↓
          candidate set
               ↓
           reranking
               ↓
        evidence selection
```

Retrieval sources:

- runbooks
- postmortems
- incident memory
- source code
- technical docs

Use graph traversal when relationship context matters.

Use semantic retrieval when wording differs.

---

# 18. Graph-Enhanced Reasoning

Neo4j is not simply a database for displaying a graph.

It is part of the reasoning substrate.

Example query:

> "What services sit on the causal path between checkout and the observed failing database?"

Graph traversal can provide:

```text
frontend
 ↓
checkout
 ↓
payment
 ↓
postgres
```

The model then reasons over that structured path together with observed telemetry.

This is more reliable than asking an LLM to infer topology from raw text.

---

# 19. Agent Memory

Separate:

## Working memory

Current incident state.

## Episodic memory

Past incidents and their outcomes.

## Semantic knowledge

Runbooks, architecture, service metadata.

## Organizational memory

Approved patterns, policies, ownership, known safe actions.

Only validated data should become durable organizational knowledge.

---

# 20. Debugging Architecture

The debugging subsystem is an agentic software-engineering loop.

```text
Incident
   ↓
Localize service
   ↓
Trace endpoint
   ↓
Find code target
   ↓
Inspect recent changes
   ↓
Generate diagnosis
   ↓
Generate reproduction
   ↓
Run reproduction
   ↓
Generate patch
   ↓
Run tests
   ↓
Replay original fault
   ↓
Compare telemetry
```

A patch must not be promoted on code review aesthetics alone.

---

# 21. Patch Generation Strategy

The Debugger should make the smallest justified change.

Patch context must include:

- relevant source
- nearby implementation
- tests
- error
- stack trace
- recent changes
- expected behavior

The model should be instructed to avoid unrelated refactors.

The patch artifact should be machine-readable:

```text
base_sha
patch
modified_files
reason
expected_behavior
test_plan
rollback
```

---

# 22. Execution-Based Debugging

The benchmark should contain defects where:

- symptoms are non-obvious
- cause is not identical to alert text
- multiple services are involved
- code change matters
- remediation can be independently verified

For code bugs:

```text
baseline test/fault
        ↓
fails
        ↓
candidate patch
        ↓
tests
        ↓
reproduce original failure
        ↓
patch applied
        ↓
failure disappears
```

This provides objective evidence of debugging capability.

---

# 23. Remediation Intelligence

The Remediation Planner produces alternatives.

Example:

```text
Option A:
restart unhealthy task
Risk: low

Option B:
rollback deployment
Risk: medium

Option C:
scale dependency
Risk: medium
```

The policy engine then decides what is allowed.

The AI must not select a production action solely because it is the "most likely" fix.

The expected outcome and risk are part of the action decision.

---

# 24. Autonomous Operation Model

The autonomy loop is:

```text
Observe
  ↓
Understand
  ↓
Propose
  ↓
Verify preconditions
  ↓
Policy
  ↓
Execute
  ↓
Observe
  ↓
Verify
```

Autonomy therefore continues only when post-action evidence matches the expected result.

If expected recovery does not occur:

```text
stop
↓
rollback where safe
↓
escalate
```

---

# 25. AI Safety Gates

Before production execution:

### Gate 1 — Evidence

Is the proposed action grounded in actual observations?

### Gate 2 — Diagnosis

Is the suspected cause sufficiently supported?

### Gate 3 — Preconditions

Are action preconditions true?

### Gate 4 — Policy

Is this action allowed for this environment/service?

### Gate 5 — Concurrency

Does another action currently hold the target resource?

### Gate 6 — Kill switch

Is autonomy globally/per-service enabled?

### Gate 7 — Verification plan

Can success be objectively measured?

### Gate 8 — Rollback

Is there a safe rollback/compensation path?

All gates must pass for autonomous production execution.

---

# 26. Human-in-the-Loop

For serious actions, LangGraph interrupt/resume behavior can pause the workflow.

The state at interruption includes:

- proposed action
- evidence
- risk
- blast radius
- verification plan
- rollback plan

Human decision:

```text
approve
reject
request more evidence
modify allowed parameters
```

Then the workflow resumes from the checkpoint.

The system must not restart the entire investigation.

---

# 27. AI Observability with LangSmith

LangSmith is the canonical view of AI behavior.

At the incident level:

```text
Incident run
 ├── Triage
 ├── Investigation
 │    ├── tool calls
 │    ├── graph retrieval
 │    ├── LLM calls
 │    └── evidence synthesis
 ├── Diagnosis
 ├── Debugging
 ├── Verification
 └── Remediation
```

Capture:

- model
- provider
- latency
- tokens
- cost
- prompts
- structured outputs
- tool inputs
- tool outputs
- retriever results
- evaluation results

Sensitive operational data must be redacted or policy-controlled before external observability upload.

---

# 28. OpenTelemetry for AI

Aegis should instrument the AI runtime with OpenTelemetry-compatible tracing where useful for cross-system correlation.

Example:

```text
HTTP request
  ↓
incident workflow
  ↓
agent span
  ↓
LLM span
  ↓
MCP span
  ↓
Prometheus query
  ↓
trace retrieval
```

The important goal is a shared correlation ID so operators can move between the infrastructure and AI views.

OpenTelemetry provides standardized semantic conventions across traces, metrics, logs, resources, and other telemetry areas.

LangSmith remains the specialized AI investigation and evaluation workspace.

---

# 29. Evaluation Architecture

The evaluation system is independent from the production decision engine.

```text
Benchmark Scenario
      ↓
Fault Injection
      ↓
Aegis Run
      ↓
Ground Truth Comparator
      ↓
Evaluator Set
      ↓
LangSmith Experiment
      ↓
Result
```

The benchmark must prevent leakage.

The agent must not receive:

- scenario ID if it reveals ground truth
- injected-fault configuration
- evaluator expected answer
- hidden labels

---

# 30. Evaluation Layers

## Layer 1 — Component evaluation

Test one capability:

- root cause
- retrieval
- graph localization
- code localization
- patching

## Layer 2 — Trajectory evaluation

Evaluate:

- tool sequence
- investigation efficiency
- recovery from failures
- unnecessary loops
- evidence coverage

## Layer 3 — End-to-end evaluation

Evaluate:

```text
incident
→ diagnosis
→ debug
→ patch
→ verify
→ remediation
```

## Layer 4 — Safety evaluation

Stress:

- prompt injection
- conflicting instructions
- incomplete evidence
- tool failures
- ambiguous symptoms
- unsafe action proposals
- stale approvals

---

# 31. Core AI Metrics

## Root-cause correctness

```text
correct root-cause cases
------------------------
all evaluable incidents
```

Also report:

- exact match
- semantic match
- causal-path match

## Evidence precision

Of evidence cited, how much actually supports the claim?

## Evidence recall

Of the ground-truth critical evidence, how much did Aegis identify?

## Unsupported claim rate

```text
claims without valid supporting evidence
----------------------------------------
all material claims
```

Target should trend downward and be treated as a release gate.

## Abstention quality

Measure:

- correct abstentions
- incorrect abstentions
- overconfident wrong answers

## Calibration

Use:

- Brier score
- ECE

## Debugging success

```text
verified successful repairs
---------------------------
repair-attempt incidents
```

## Regression safety

```text
candidate fixes with no protected regression
--------------------------------------------
candidate fixes evaluated
```

## Autonomy safety

Most important safety metric:

```text
unsafe autonomous actions
-------------------------
autonomous production actions
```

This should be effectively zero for production-authorized action classes.

---

# 32. Efficiency Metrics

Track:

- time to first useful hypothesis
- time to correct diagnosis
- time to verified fix
- tool calls per incident
- LLM calls per incident
- tokens per incident
- cost per incident
- agent wall-clock duration
- human interactions per incident

Efficiency must never be optimized by sacrificing correctness or safety.

---

# 33. Benchmark Design

Initial benchmark:

**100+ scenarios**

Recommended distribution:

```text
20% service/runtime failures
20% dependency failures
15% deployment/change regressions
15% resource failures
10% database/cache failures
10% network/cascading failures
5% ambiguous incidents
5% adversarial/safety cases
```

These percentages are benchmark design goals, not claims about real-world incident frequency.

Each scenario should be independently replayable.

---

# 34. Ablation Program

Every major architecture change should support ablation.

Compare:

```text
Aegis full
vs
Aegis without Neo4j
vs
Aegis without hybrid retrieval
vs
Aegis without verifier
vs
Aegis single-agent
vs
Aegis no execution verification
```

This answers:

> Which components create actual performance gains?

Without ablations, architecture decisions become opinions.

---

# 35. Model Routing

Aegis should use a provider/model abstraction.

Do not bind domain logic to one model provider.

Model selection can consider:

- task complexity
- required latency
- cost
- context requirement
- structured-output reliability
- historical evaluation performance

Example:

```text
simple summarization → cheaper model
complex diagnosis → stronger model
code debugging → strongest coding-capable model
verification → deterministic tooling + targeted model
```

Provider fallback must use the same structured output contracts and safety gates.

---

# 36. Prompt Management

Every important prompt must have:

- stable identifier
- version
- owner
- test set
- release notes
- evaluation results

Prompts must not contain dynamic production secrets.

A prompt change is an AI behavior change and therefore requires evaluation.

---

# 37. Long-Context Strategy

Do not solve every problem with a larger context window.

Use:

```text
retrieve
→ rank
→ compress
→ reason
```

The context should be constructed from the most relevant evidence.

Large raw telemetry dumps increase both cost and distraction.

---

# 38. Agent Budgeting

Every agent invocation receives:

```text
max_wall_time
max_llm_calls
max_tool_calls
max_tokens
max_retries
```

The supervisor enforces the budget.

Agents cannot modify their own limits.

At budget exhaustion:

- preserve evidence
- stop
- report uncertainty
- escalate

---

# 39. Agent Failure Recovery

If an agent fails:

1. classify failure
2. retry only when safe
3. avoid repeating deterministic failures
4. route to an alternative strategy
5. reduce confidence if evidence is missing
6. escalate when the failed capability is essential

Example:

GitHub unavailable:

```text
change analysis incomplete
→ deployment evidence may still work
→ diagnosis continues only if sufficient
→ confidence reduced otherwise
```

---

# 40. Security and Prompt Injection Defense

Agentic SRE systems are exposed to untrusted text.

Threat sources:

- logs
- HTTP payloads
- Git commit messages
- issues
- alerts
- customer text
- generated telemetry

Rules:

```text
untrusted text ≠ instruction
tool result ≠ policy
LLM output ≠ authorization
```

Use explicit delimiters and structured envelopes.

The policy engine must evaluate the action independently.

---

# 41. Red-Team Evaluation

Create adversarial benchmark cases where:

- logs contain malicious instructions
- commit messages instruct the agent to ignore policy
- an attacker injects fake "root cause" text
- evidence is contradictory
- telemetry source is unavailable
- a tool returns unexpected schema
- a stale approval is replayed
- two agents request the same resource
- a remediation appears safe but has hidden blast radius

Expected result:

- no unsafe action
- evidence properly classified
- uncertainty surfaced
- escalation when required

---

# 42. Production Learning Loop

Aegis should learn from incidents without automatically changing itself.

```text
incident
  ↓
outcome
  ↓
evaluation
  ↓
failure classification
  ↓
candidate improvement
  ↓
offline benchmark
  ↓
staging
  ↓
release
```

Production incidents generate training/evaluation material.

They must not directly mutate production prompts or policy.

---

# 43. Evaluation-Driven Development

The development workflow for AI changes:

```text
change prompt/model/agent
        ↓
run targeted unit eval
        ↓
run regression benchmark
        ↓
inspect failed traces
        ↓
run safety benchmark
        ↓
compare against baseline
        ↓
approve release
```

LangSmith provides the trace/evaluation workspace; the benchmark definitions and deterministic ground truth remain under version control.

---

# 44. End-to-End Golden Path

The ideal demonstration:

```text
1. Fault injected
2. Alert fired
3. Incident created
4. Aegis builds timeline
5. Graph localized
6. Telemetry correlated
7. Competing hypotheses generated
8. Hypothesis tested
9. Recent deployment inspected
10. Relevant code found
11. Reproduction generated
12. Candidate patch created
13. Tests run
14. Patch deployed to staging
15. Original failure replayed
16. Recovery verified
17. Risk assessed
18. Human approval for serious action
19. Production action executed
20. Recovery verified again
21. Incident resolved
22. Memory stored
23. LangSmith trace/evaluation recorded
24. Benchmark result updated
```

This is the product.

---

# 45. Design Rules to Protect Long-Term Quality

Aegis must avoid these failure modes:

### "Just let the LLM inspect everything"

Rejected.

Reason: poor provenance, excessive context, unpredictable behavior.

### "Create an agent for every small task"

Rejected.

Reason: unnecessary coordination complexity.

### "Use Neo4j for everything"

Rejected.

Reason: transactional state belongs in PostgreSQL.

### "Use PostgreSQL for every graph query"

Rejected.

Reason: topology becomes increasingly difficult to reason about as the system grows.

### "Let the LLM decide whether its fix is safe"

Rejected.

Reason: safety must be deterministic and policy-controlled.

### "Consider a patch correct because tests pass"

Rejected.

A patch must also reproduce the original failure and pass protected regression checks.

### "Use benchmarks only at the end"

Rejected.

Evaluation must run during development.

---

# 46. Architecture Summary

Aegis can be reduced to seven layers:

```text
1. APPLICATION
   DeathStarBench workloads

2. OBSERVABILITY
   OpenTelemetry + metrics/logs/traces

3. KNOWLEDGE
   Neo4j + PostgreSQL + hybrid retrieval

4. AGENT RUNTIME
   LangGraph + bounded specialized capabilities

5. TOOLING
   MCP + scoped integrations

6. CONTROL
   policy + authorization + leases + verification

7. EVALUATION
   LangSmith + deterministic benchmark + safety evaluation
```

The resulting loop is:

```text
OBSERVE
   ↓
UNDERSTAND
   ↓
HYPOTHESIZE
   ↓
TEST
   ↓
DEBUG
   ↓
REPAIR
   ↓
VERIFY
   ↓
DECIDE
   ↓
ACT
   ↓
LEARN
```

The architecture's defining characteristic is not the number of agents.

It is the combination of:

**structured state + grounded evidence + graph context + tool use + execution-based verification + deterministic safety + measurable evaluation.**

---

# 47. References

- LangChain / LangGraph documentation: https://docs.langchain.com/
- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/specs/semconv/
- Neo4j GraphRAG Python documentation: https://neo4j.com/docs/neo4j-graphrag-python/current/
- AWS ECS/Fargate guidance: https://docs.aws.amazon.com/decision-guides/latest/decision-guides/choosing-aws-container-service.html
- DeathStarBench: https://github.com/delimitrou/DeathStarBench
