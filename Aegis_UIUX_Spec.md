# Aegis UI/UX Specification
## AI-Native SRE Control Plane — Product Interface & Experience Specification

**Version:** 2.0  
**Status:** Design baseline  
**Date:** September 2026  
**Audience:** Product, design, frontend engineering, AI engineering, SRE/domain reviewers  
**Primary reference product studied:** incident.io, with emphasis on its 2026 navigation, Response/On-call information architecture, Investigations experience, timeline model, AI/coding workflow, Insights, and operational UX patterns.  
**Aegis visual direction:** Pure-black, premium, minimal, high-density, enterprise-grade.  
**Design principle:** Aegis should feel like a calm flight deck for production systems, not a generic SaaS dashboard and not an AI chat application.

---

# 1. Purpose

This document defines the UI and UX system for Aegis.

Aegis is an AI-native SRE platform that observes distributed systems, investigates incidents, understands production topology, debugs application code, validates candidate fixes, recommends or executes remediation, and continuously measures the quality of its own AI behavior.

The interface must therefore solve a different problem from a conventional dashboard.

A conventional dashboard asks:

> "What metrics do I have?"

Aegis must answer:

> "What is happening, what does Aegis believe is happening, why does it believe that, what has it verified, what is it doing now, and what should I do next?"

The interface is a decision surface.

The design must make it possible for a responder to understand a severe incident within seconds without opening six external tools.

---

# 2. Research Findings From incident.io

The following are research observations, not instructions to reproduce incident.io visually.

## 2.1 Navigation evolved toward product-oriented sections

In July 2026, incident.io documented a move away from a tabs-heavy model toward expanded left navigation, with Response and On-call as distinct product areas. It also introduced My Tasks, customizable saved home views, breadcrumbs, and better settings navigation.

Implication for Aegis:

- use a persistent left rail
- group capabilities by operator goal rather than implementation
- make Incident Response the primary operating area
- keep Reliability / Engineering / Governance distinct
- provide global search/command navigation
- use breadcrumbs on deep pages
- provide a personal task surface

Source:
https://incident.io/changelog/upcoming-navigation-changes

---

## 2.2 Incident timelines are a central source of truth

incident.io's alert timeline exposes the full lifecycle of an alert and its relationship to incidents. Its broader response timeline captures operational activity chronologically.

Implication for Aegis:

The timeline should not be a decorative feed.

It must be one of the primary information structures.

Aegis's timeline must combine:

- alerts
- telemetry discoveries
- graph findings
- hypothesis changes
- tool calls
- code discoveries
- test execution
- remediation decisions
- approvals
- deployment events
- verification
- human activity

Every important event should be traceable.

---

## 2.3 Investigations are designed to run in parallel with humans

In April 2026, incident.io described Investigations as beginning when an incident starts, checking recent deploys, telemetry, previous incidents, code and Slack context while engineers continue working independently.

The documented UX goal is explicitly to avoid waiting for the AI.

Implication for Aegis:

Aegis must never make the responder wait for the AI to finish before they can work.

The interface needs:

- live progress
- partial findings
- current hypothesis
- completed checks
- pending checks
- suggested next actions
- direct links into code/debugging
- ability to keep working while AI continues

Source:
https://incident.io/blog/how-it-feels-to-run-an-incident-with-ai-sre

---

## 2.4 AI needs a visible operational status

The 2026 Investigations experience includes live investigation progress in mobile chat and improved visibility of context added to investigations.

Implication:

Aegis should expose a compact AI status strip:

`Investigating · 4/7 evidence paths · next: deployment correlation`

This is more useful than a generic animated "AI is thinking..." indicator.

Source:
https://incident.io/changelog/shard-alert-source-rate-limits

---

## 2.5 Coding workflow should be connected to the incident

incident.io's 2026 Investigations article describes moving from Slack/incident context directly into Claude Code, keeping the investigation synchronized while code debugging continues.

Implication:

Aegis should treat:

`Incident ↔ Investigation ↔ Code`

as a single workflow.

The user should be able to open a Debug Workbench pre-populated with:

- incident ID
- suspected service
- trace
- stack trace
- affected endpoint
- relevant commit
- source files
- hypotheses
- reproduction plan
- verification criteria

Source:
https://incident.io/blog/how-it-feels-to-run-an-incident-with-ai-sre

---

## 2.6 Incident communication is a first-class UX problem

incident.io has invested in updates, sharing drawers, external status-page drafts, call notes, Slack-native interactions, and stakeholder communication.

Implication:

Aegis must separate:

- engineering evidence
- incident commander summary
- executive update
- external customer communication

One source of incident truth should generate multiple audience-specific presentations.

---

## 2.7 AI quality needs its own interface

incident.io's 2026 Investigations updates include a homepage showing 90-day performance windows and trends for accuracy and engagement.

Implication:

Aegis should expose an explicit AI Evaluation area.

It should not hide model quality behind a settings page.

A user should be able to answer:

- How accurate has Aegis been?
- Is accuracy improving?
- Where does it fail?
- Which model is being used?
- Which agent is causing failures?
- Is a new release better?
- Is autonomy becoming safer?

Source:
https://incident.io/changelog/shard-alert-source-rate-limits

---

## 2.8 Integration health should be visible

incident.io documents health visibility for Nexus connections and telemetry sources.

Implication:

Aegis should have a system-health surface showing:

- telemetry source health
- MCP health
- graph health
- Git provider health
- model provider health
- evaluator health

"Nothing found" and "source unavailable" must never look identical.

---

# 3. Product Design Thesis

Aegis should visually communicate five qualities:

**Calm**
The UI should reduce cognitive load during incidents.

**Precise**
Numbers, evidence and timestamps should be easy to scan.

**Intelligent**
AI should feel integrated into the workflow rather than bolted onto it.

**Powerful**
The interface should expose real system relationships and engineering detail.

**Trustworthy**
The UI must make it obvious why Aegis believes something and why an action is allowed.

The product must feel premium without becoming decorative.

---

# 4. Visual Direction

## 4.1 Base canvas

The primary application background is:

`#000000`

No blue-tinted dark gray page background.

No gradient page background.

No large colored blobs behind content.

Pure black is intentional.

---

## 4.2 Surface hierarchy

Use very subtle near-black surfaces:

```text
Canvas        #000000
Surface 1     #050505
Surface 2     #080808
Surface 3     #0D0D0D
Surface 4     #111111
```

Borders:

```text
Hairline      #151515
Standard      #1C1C1C
Emphasis      #292929
```

Borders should be low-contrast.

Do not outline every component.

---

## 4.3 Typography

The requested visual language is closer to modern premium technology products and Uber-like operational clarity than to traditional enterprise software.

Primary family:

```text
Inter
-apple-system
BlinkMacSystemFont
"Helvetica Neue"
Arial
sans-serif
```

The design must work correctly even when Inter is unavailable.

Numeric and log content:

```text
ui-monospace
SFMono-Regular
Menlo
Monaco
Consolas
monospace
```

### Typography hierarchy

Display:

`56–72 px`

Page title:

`34–52 px`

Section title:

`15–18 px`

Card heading:

`11–13 px`

Body:

`12–14 px`

Metadata:

`9–11 px`

Telemetry/code:

`9–11 px`

Do not use tiny 7px interface text as the normal UX.

---

# 5. Logo and Brand Direction

## 5.1 Brand concept

Aegis means a shield/protective system.

The mark should communicate:

- protection
- infrastructure
- intelligence
- precision

Avoid:

- generic shield clip-art
- obvious AI sparkle
- hexagon-only logo
- robot head
- circuit-board clichés
- complicated line art

---

## 5.2 Recommended mark

Use a minimal geometric shield with an embedded negative-space A.

Concept:

```text
       /\
      /  \
     / /\ \
    / /  \ \
    \ \  / /
     \ \/ /
      \  /
       \/
```

But the final geometry should be custom and optically balanced.

The mark must work in:

- 16px favicon
- 20px nav
- 28px header
- 64px marketing
- monochrome print

Primary application mark:

`#FFFFFF` on `#000000`

Secondary brand expression may use a subtle spectral/green highlight only where it carries semantic meaning.

---

# 6. Global Layout

Desktop target:

`1440–1728 px`

Minimum supported:

`1280 px`

Mobile:

`375 px`

Tablet:

`768 px`

---

## 6.1 Application frame

```text
┌──────────────────────────────────────────────────────────────┐
│ top global bar                                               │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│ persistent   │ primary workspace                             │
│ navigation   │                                               │
│              │                                               │
│              │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

Recommended desktop widths:

```text
Expanded sidebar    232–248 px
Collapsed sidebar    64–72 px
Top bar              52–60 px
Main content         flexible
```

The sidebar must be collapsible.

---

# 7. Primary Information Architecture

The navigation should be goal-oriented.

```text
AEGIS

COMMAND
  Home
  Incidents
  Live Systems
  My Tasks

RELIABILITY
  Service Graph
  Reliability
  Recurring Failures
  Runbooks

ENGINEERING
  Investigations
  Debug Workbench
  Deployments
  Code Intelligence

GOVERNANCE
  Approvals
  AI Evaluation
  Audit
  Policies

CONFIGURATION
  Integrations
  Environments
  Teams
  Settings
```

The word "AI" should not dominate every navigation item.

AI is the engine inside the product.

---

# 8. Global Top Bar

The global top bar contains:

Left:

- breadcrumbs
- current environment

Right:

- global search
- command shortcut
- active incident indicator
- autonomy status
- notifications
- profile

Example:

```text
Incidents / INC-2847        [⌘K Search]   ● Guarded   3 alerts   SK
```

---

# 9. Command Palette

Shortcut:

`⌘K`

Windows/Linux:

`Ctrl+K`

Search across:

- incidents
- services
- deployments
- repositories
- teams
- runbooks
- graph entities
- actions
- pages

Command actions:

```text
Open incident
Investigate service
Open service graph
Open deployment
Start debugging
Search evidence
View approvals
Pause autonomy
Open evaluation run
```

The command palette should support fuzzy search.

---

# 10. Home / Command Center

The homepage is not primarily a chart dashboard.

It is an operator cockpit.

Top section:

```text
GOOD MORNING, SARAVAN

3 active incidents
1 awaiting approval
2 reliability risks
4 AI investigations running
```

Then:

### Active incidents

Large but compact incident cards.

### My tasks

Tasks requiring attention:

- approval
- unresolved investigation
- failed verification
- policy violation
- stale follow-up
- unhealthy integration

### System health

Compact status grid:

```text
Telemetry       ●
Graph           ●
Git             ●
MCP             ●
AI provider     ●
Evaluation      ●
```

### Reliability trends

Use sparklines, not giant charts.

---

# 11. Incident List

The incidents list should be dense and extremely scannable.

Columns:

```text
Severity
Incident
Service
State
Impact
AI confidence
Duration
Owner
Updated
```

Example:

```text
P1  Checkout latency cascade
    payment-service
    Investigating
    18.7% errors
    86%
    12m
    Platform
    48s ago
```

Use row-level hover actions.

Avoid excessive cards.

Tables are preferable when the user is triaging many incidents.

---

# 12. Incident Detail — Primary Product Surface

The incident detail page is the most important page in Aegis.

It must answer five questions immediately:

1. What is happening?
2. How bad is it?
3. What does Aegis think is causing it?
4. What has Aegis proven?
5. What needs to happen next?

---

# 13. Incident Header

Example:

```text
P1  INC-2847

Checkout latency cascade

payment-service is the strongest causal origin.
A candidate fix has been verified in staging.

[Investigating] [12m 43s] [6 services] [Guarded]
```

Right actions:

```text
Replay
Share
Call
Debug
Actions
Resolve
```

Do not show 12 competing buttons.

Primary action depends on incident state.

---

# 14. Incident Severity Treatment

Severity is semantic, not decorative.

Recommended:

```text
P1 → red
P2 → orange/amber
P3 → yellow
P4 → muted gray
```

Do not make the entire screen red for P1.

Red should be reserved for:

- severe impact
- dangerous action
- failure
- urgent status

---

# 15. Incident Executive Summary

Immediately below the header:

```text
WHAT WE KNOW

payment-service is the first consistently degraded service.
Checkout p99 rose from 330ms → 2.84s.
A deployment 7m before the anomaly modified retry/pool behavior.
The failure was reproduced in staging.
```

This is not an AI chat bubble.

It is a structured summary.

Each statement should be clickable to its evidence.

---

# 16. Aegis Investigation Strip

A persistent compact AI status area:

```text
● Investigating

4 / 7 evidence paths complete

✓ traces
✓ metrics
✓ topology
✓ deployment
○ historical incidents
○ source analysis
○ verification

Next:
Inspect payment/client.py
```

Clicking expands into the investigation workspace.

---

# 17. Core Incident Workspace

Recommended structure:

```text
┌─────────────────────────────────────────────────────────────┐
│ incident header                                             │
├─────────────────────────────────────────────────────────────┤
│ impact metrics                                               │
├──────────────────────────────────────┬──────────────────────┤
│                                      │                      │
│ Evidence / Hypothesis workspace      │ Live incident       │
│                                      │ timeline             │
│                                      │                      │
├──────────────────────────────────────┴──────────────────────┤
│ Service topology / graph                                  │
├──────────────────────────────────────┬──────────────────────┤
│ Verified repair                       │ Action / approval   │
└──────────────────────────────────────┴──────────────────────┘
```

The exact proportions can shift by incident state.

---

# 18. Impact Strip

Use 4–6 compact metrics.

Example:

```text
ERROR RATE        P99               AFFECTED
18.7%             2.84s             6 / 14

TRAFFIC           CONFIDENCE        DURATION
2,418 req/min     86%               12m 43s
```

Each metric should open the relevant data explorer.

Do not show fake precision.

---

# 19. Evidence Explorer

Evidence must appear as first-class objects.

Each evidence item:

```text
SOURCE
TRACE

CLAIM
Timeout begins at payment-service

OBSERVATION
checkout → payment
0.18s → 2.71s

STATUS
VALIDATED

SOURCE
trace_id / timestamp
```

Clicking the evidence should open a contextual drawer rather than navigate away.

---

# 20. Evidence Provenance

Every AI-generated claim should expose:

```text
Supported by 4 evidence items
```

Click:

```text
Evidence E12
Evidence E18
Evidence E21
Evidence E33
```

Then show:

- source
- timestamp
- raw excerpt
- query
- retrieval path
- validation status

The user should never have to trust a citation label blindly.

---

# 21. Hypothesis Workspace

Use a hypothesis stack rather than a single AI conclusion.

Example:

```text
01  CONNECTION POOL REGRESSION        86%
    ● strongest

02  POSTGRES SATURATION                31%

03  REDIS DEGRADATION                   4%
    rejected
```

Each hypothesis supports expansion.

Expanded view:

```text
Hypothesis
Confidence
Supporting evidence
Contradicting evidence
Predictions
Tests performed
Tests remaining
Why alternatives were rejected
```

---

# 22. Confidence Visualization

Never use a giant circular AI confidence gauge.

Prefer:

- compact percentage
- horizontal meter
- calibration indicator
- text explaining evidence coverage

Example:

```text
86% confidence
████████████████░░░░

5 supporting
1 contradicting
2 tests passed
1 test pending
```

---

# 23. Hypothesis History

The UI should show confidence movement.

```text
10:31   42%
10:33   61%
10:35   73%
10:37   86%
```

A small sparkline is ideal.

This makes reasoning changes visible.

---

# 24. Service Graph

The graph is a major Aegis differentiator.

Do not make it a decorative bubble diagram.

The graph should encode:

- call relationships
- service ownership
- health
- traffic
- latency
- dependency path
- deployment changes
- incident impact
- suspected origin

---

# 25. Graph Interaction

Hover:

- health
- latency
- error rate
- owner
- current version

Click:

- open service
- open incident context
- open dependency details

Select path:

```text
checkout
  ↓
payment
  ↓
postgres
```

Then show:

```text
WHY THIS PATH MATTERS
```

with telemetry backing.

---

# 26. Causal Path Mode

Aegis should have a dedicated visual mode:

```text
Observed symptom
      ↓
checkout-service
      ↓
payment-service   ← suspected origin
      ↓
postgres
```

Edges can show:

- latency propagation
- error propagation
- traffic
- evidence strength

The graph should dim irrelevant services.

This is more useful during incidents than showing all 100 nodes at equal visual weight.

---

# 27. Timeline UX

The timeline should feel like a forensic record.

Each event:

```text
10:35:18

HYPOTHESIS TEST

Connection pool exhaustion reproduced
in staging.

Evidence:
trace E21
metric E18

Result:
PASS
```

Types:

```text
ALERT
OBSERVATION
AI
HYPOTHESIS
TEST
CODE
ACTION
APPROVAL
DEPLOY
VERIFICATION
HUMAN
```

Use tiny semantic markers, not giant icons.

---

# 28. Timeline Filtering

Filters:

```text
All
AI
Telemetry
Code
Actions
Humans
Communication
```

Advanced filters:

```text
agent
source
service
time
confidence
action
```

The timeline should support "show only what changed."

---

# 29. Investigation Workspace

Dedicated route:

`/incidents/:id/investigation`

Layout:

```text
left:
  investigation plan

center:
  evidence + findings

right:
  live agent activity
```

Investigation plan:

```text
✓ Establish impact
✓ Map dependency path
✓ Check recent changes
✓ Compare historical incidents
→ Inspect payment client
○ Reproduce failure
○ Verify patch
```

The plan should update live.

---

# 30. Agent Activity UX

Never display raw chain-of-thought.

Show operational summaries only.

Good:

```text
Topology Analyst
Found a 3-hop path from checkout to postgres.
12 related traces inspected.
```

Bad:

```text
I am thinking about whether...
Maybe I should...
I believe...
```

Aegis should expose what the agent did, not private internal reasoning.

---

# 31. Agent Activity Detail Drawer

Click agent event:

```text
AGENT
Topology Analyst

TASK
Determine the likely dependency path.

TOOLS
graph.find_causal_paths
traces.query_spans
metrics.query_range

RESULT
Payment-service is the first degraded hop.

EVIDENCE
E12
E18
E21

DURATION
1.34s
```

This is ideal for auditability and debugging agent behavior.

---

# 32. Debug Workbench

This is where Aegis should exceed typical incident-management UX.

Layout:

```text
┌───────────────────────────────────────────────────────────┐
│ Incident context                                          │
├────────────┬──────────────────────────────┬───────────────┤
│ Repo tree  │ Code editor                  │ AI diagnosis   │
│            │                              │               │
│ files      │ payment/client.py            │ Evidence       │
│ symbols    │                              │ Hypothesis     │
│ tests      │                              │ Suggested fix  │
│            │                              │ Tests          │
├────────────┴──────────────────────────────┴───────────────┤
│ Test / reproduce / sandbox results                         │
└───────────────────────────────────────────────────────────┘
```

---

# 33. Debug Context Header

```text
INC-2847

payment-service / payment/client.py

Suspected:
connection-pool regression

Changed:
8f3a2c

Reproduction:
PASS

Tests:
47 / 47

Staging:
PASS
```

This makes the debugging state immediately legible.

---

# 34. Code Intelligence UX

Use code lenses:

```text
Changed in incident window
Likely execution path
Covered by test
Referenced by trace
```

Examples:

```text
payment/client.py:88

INC-2847
3 trace references
1 deployment change
2 failing requests
```

The user should be able to move from an evidence item directly to the relevant line.

---

# 35. Patch Review

Patch view should look closer to a premium Git client than a chatbot.

```text
Candidate repair

91d4e1

2 files changed
+ 7
- 3

Reason
Release connection before retry.

Evidence
E12 E18 E33

Verification
47/47 tests
reproduction fixed
staging healthy
```

Primary actions:

```text
Inspect
Run verification
Promote
Reject
```

---

# 36. Verification UX

Verification should be explicit.

```text
PATCH VERIFICATION

Original failure
PASS reproduced

Unit tests
47 / 47 PASS

Integration tests
12 / 12 PASS

Staging
HEALTHY

P99
2.71s → 0.31s

5xx
18.4% → 0.0%

Regression
NONE DETECTED
```

Use before/after visual comparisons.

---

# 37. Risk Decision Surface

Never hide an action's risk in a tiny badge.

Example:

```text
PRODUCTION ACTION

Rollback payment-service deployment

RISK
MEDIUM

WHY
Affects 3 downstream request paths.

BLAST RADIUS
payment-service
checkout-service
order-service

VERIFICATION
Available

ROLLBACK
Available

POLICY
Human approval required
```

Primary controls:

```text
Approve
Reject
Request more investigation
```

---

# 38. Autonomous Actions

When Aegis can act automatically:

```text
AUTO ACTION

Restart payment-service task

Policy: Tier 1
Blast radius: 0 dependent services
Confidence: 98%
Rollback: automatic
Verification: enabled

[Allow once] [Disable autonomy]
```

The user should see the policy and why the action qualified.

---

# 39. Approval UX

Approval is a decision, not a modal checkbox.

A serious approval drawer should contain:

```text
What will happen
Why it will happen
What evidence supports it
What can go wrong
What will be affected
How it will be verified
How it will be reversed
Who requested it
When the proposal expires
```

The final action button should state the actual consequence.

Example:

`Approve rollback to 8f3a2c`

not:

`Confirm`

---

# 40. Safety Controls

Dedicated page:

`Governance → Audit & Policy`

Sections:

```text
Autonomy
Kill switch
Action tiers
Service allowlists
Rate limits
Blast-radius policies
Approval requirements
Audit log
```

Global kill switch:

large, obvious, isolated control.

Do not make it permanently red.

It should become visually urgent only when engaged.

---

# 41. Communication Center

Aegis should have a multi-audience communication composer.

Tabs:

```text
Engineering
Incident Commander
Leadership
Customer / Status
```

Each generated update shows:

```text
AI drafted
Evidence-backed
Last updated 10:38:21
```

Buttons:

```text
Edit
Approve
Publish
```

External communications should never auto-publish by default.

---

# 42. Incident Calls

When an incident call exists:

```text
CALL ACTIVE · 18:42

Participants  6
Scribe        ON
Decisions     3
Action items  4
```

Call notes should appear in a collapsible drawer.

The incident timeline should automatically incorporate:

- decisions
- actions
- ownership
- important statements

---

# 43. My Tasks

Inspired by the move toward explicit task management in modern incident platforms.

Aegis tasks:

```text
3 approvals
1 failed verification
2 follow-ups
1 unresolved investigation
1 integration health issue
```

Sort:

- urgent
- due soon
- blocked
- assigned to me

The home page should not become a passive reporting surface.

It should tell the user what requires action.

---

# 44. Live Systems

`Command → Live Systems`

Shows current operational state.

Views:

```text
Services
Containers
Databases
Queues
Dependencies
Deployments
```

Service row:

```text
payment-service

health        degraded
error rate    4.2%
p99           821ms
version       91d4e1
owner         Payments
incidents     2
```

---

# 45. Service Detail Page

A service page becomes its long-term operational identity.

Sections:

```text
Overview
Dependencies
Incidents
Deployments
Telemetry
Code
Runbooks
Reliability
AI knowledge
```

Header:

```text
payment-service
Payments Team
Production

● Operational
version 91d4e1
```

---

# 46. Deployment Detail

Show:

```text
Deployment
91d4e1

Service
payment-service

Started
10:24

Duration
4m 12s

Changed files
3

Linked incidents
INC-2847

Risk
Medium
```

Timeline:

```text
build
→ test
→ deploy
→ telemetry change
→ incident
```

Aegis should make change-impact relationships visually obvious.

---

# 47. Reliability Overview

Do not make this an analytics-only dashboard.

Metrics:

```text
Incident frequency
MTTR
Time-to-diagnosis
Time-to-fix
Recurrence rate
Alert noise
Manual interventions
Automation success
```

Break down by:

- service
- team
- incident type
- severity
- time
- deployment

---

# 48. Recurring Failures

This is one of Aegis's long-term intelligence surfaces.

Example:

```text
Recurring pattern

Connection pool exhaustion

7 incidents
4 services
38 manual interventions
3 similar deployments

Aegis recommendation:
Create a reliability control for connection acquisition.
```

The user should be able to inspect the graph of similar incidents.

---

# 49. Incident Memory UX

Memory should not look like a chatbot history.

Use structured knowledge cards.

```text
KNOWN FAILURE

Connection pool exhaustion

Seen:
7 times

Services:
payment
billing
checkout

Typical trigger:
retry path holds connection too long

Known remediation:
release connection before retry

Verification:
pool utilization < 75%
```

Each memory should show provenance.

---

# 50. AI Evaluation Home

Route:

`Governance → AI Evaluation`

Hero:

```text
Aegis Intelligence

Accuracy       94.2%
Grounding      98.7%
Patch success  91.8%
Unsafe actions 0.0%
```

Underneath:

```text
90 day trend
```

and:

```text
Current release
2.0.0-rc3

Previous
2.0.0-rc2

Δ
+3.1 RCA points
-0.7 grounding
```

Never show only one aggregate AI score.

---

# 51. Evaluation Drill-Down

Tabs:

```text
Overview
Incidents
Root Cause
Evidence
Debugging
Tool Use
Safety
Cost
Latency
Regression
```

Example table:

```text
Scenario       Result      RCA     Grounding   Patch
SN-DB-014      PASS        100%    100%        n/a
SN-CACHE-008   PASS        92%     100%        100%
HR-NET-021     FAIL        34%     81%         n/a
```

Clicking a failed case opens the exact LangSmith trace.

---

# 52. Agent Evaluation Page

Show agents separately:

```text
Evidence Investigator    96.4%
Topology Analyst         98.1%
Change Analyst            94.7%
Diagnosis                 91.8%
Debugger                  88.3%
Verifier                  97.6%
Remediation Planner      99.2%
```

These numbers must come from the evaluation benchmark.

Do not hand-enter them.

---

# 53. AI Run Detail

Aegis should provide an internal AI-run viewer:

```text
Run #91b7

Incident INC-2847

Model:
...

Prompt:
v42

Agent:
Diagnosis

Input evidence:
17 items

Tools:
14

Output:
...

Evaluators:
Groundedness    PASS
RCA             PASS
Policy          PASS
```

The detailed LangSmith trace remains the canonical AI trace.

---

# 54. Evaluation Failure UX

A failed AI run should be useful to the engineer.

Display:

```text
FAILED EVALUATION

Expected:
redis failure

Predicted:
postgres saturation

Failure class:
CAUSALITY_FAILURE

Evidence used:
E12 E19 E22

Missing:
redis latency evidence

Suggested investigation:
Query Redis p99 over the same window.
```

This turns evaluation into engineering feedback rather than a leaderboard.

---

# 55. Integration Health Center

Route:

`Configuration → Integrations`

Cards:

```text
OpenTelemetry       HEALTHY
Prometheus           HEALTHY
Tracing              HEALTHY
Neo4j                HEALTHY
GitHub               HEALTHY
MCP                  DEGRADED
LLM Provider         HEALTHY
LangSmith            HEALTHY
```

Each source:

- last successful query
- error rate
- latency
- permissions
- scopes
- affected features

---

# 56. MCP Tool Explorer

For administrators:

```text
MCP

kubernetes
  list_pods          READ
  get_pod_logs       READ
  restart_pod        WRITE / TIER 1
  scale_deployment   WRITE / TIER 2

github
  get_commit_diff    READ
  create_branch      WRITE / CONTROLLED

neo4j
  query_graph        READ
```

This should make the tool boundary visible.

---

# 57. Environments

Aegis needs clear environment selection.

Example:

```text
LOCAL
STAGING
PRODUCTION
```

Production should have a visible semantic marker.

The environment selector must never be ambiguous.

When the user switches environments, the page should update all data accordingly.

---

# 58. Status Indicators

Semantic status tokens:

```text
Healthy       green
Investigating blue
Waiting       amber
Degraded      orange
Critical      red
Unknown       gray
```

Do not use color alone.

Every status should also have:

- text
- icon/shape where necessary
- hover detail

---

# 59. Motion System

Motion should communicate state.

Do not add animation just to impress.

## Allowed motion

- 120–180ms hover
- 180–240ms drawers
- 240–400ms page transitions
- subtle pulse for active investigation
- graph edge flow for active causal paths
- data update shimmer only when useful
- smooth expansion of evidence

## Avoid

- spinning AI brains
- excessive particles
- infinite background animations
- bouncing dashboard cards
- animated gradients behind operational content

Premium does not mean busy.

---

# 60. Signature Aegis Motion

Aegis can own one distinctive interaction:

### "Causal Trace"

When the user selects a suspected root cause:

```text
symptom
  ↓
dependency edge highlights
  ↓
service highlights
  ↓
evidence cards appear
  ↓
code target appears
  ↓
recommended repair appears
```

The UI creates a continuous visual path from:

**Signal → Cause → Code → Fix → Verification**

This should become an Aegis signature.

---

# 61. Drawer System

Use drawers heavily.

Good candidates:

- evidence
- alert details
- graph node
- deployment
- AI run
- call notes
- approval
- policy
- integration health

Why:

The responder should not lose incident context.

A drawer preserves the underlying page.

---

# 62. Modal System

Use modals only for:

- destructive actions
- final approvals
- critical confirmations
- configuration choices that require focus

Do not use modals for routine information.

---

# 63. Toast System

Toasts are for:

- saved
- copied
- queued
- connection restored

Not for:

- critical errors
- approval decisions
- important incident findings

Important state belongs in the page.

---

# 64. Empty States

Never say:

> No data.

Instead:

```text
No incidents in this view.

Last 24 hours:
0 triggered

Try:
Clear filters
```

For unavailable integrations:

```text
Telemetry unavailable

Prometheus has not responded for 38 seconds.

Aegis has reduced confidence because metric evidence is unavailable.

[Open integration health]
```

---

# 65. Error States

Error surfaces must explain:

```text
What failed
What is affected
What Aegis did instead
What the user can do
```

Example:

```text
Graph unavailable

Neo4j cannot currently be queried.

Aegis is continuing with direct telemetry,
but blast-radius analysis is incomplete.

[Retry]
[View system health]
```

---

# 66. Loading States

Avoid large centered spinners.

Use structural skeletons for pages.

For AI workflows use progress states:

```text
Investigating
Finding evidence
Testing hypothesis
```

The user should always know what the system is doing.

---

# 67. Accessibility

Requirements:

- WCAG AA target
- keyboard navigation
- visible focus
- semantic headings
- screen-reader labels
- no color-only semantics
- reduced-motion preference
- 44px minimum interactive target where touch is expected
- keyboard shortcut documentation

---

# 68. Responsive Strategy

## Desktop

Full control plane.

## Tablet

Prioritize:

```text
incident
timeline
evidence
actions
```

Graph becomes a secondary tab/drawer.

## Mobile

Not a compressed desktop.

Primary mobile workflow:

```text
Alert
→ incident summary
→ AI progress
→ evidence
→ approval
→ communication
```

Advanced graph/code views open as focused full-screen surfaces.

---

# 69. Mobile Incident View

Example:

```text
P1

Checkout latency cascade

18.7% errors
2.84s p99
6 services

Aegis:
Investigating · 4/7 checks

Strongest hypothesis:
Connection-pool regression · 86%

[Review evidence]

Proposed action:
Rollback deployment

[Approve] [Reject]
```

This should make serious incident handling possible without a laptop.

---

# 70. Density Rules

Aegis should be dense, but not cramped.

Desktop spacing:

```text
4px   micro
8px   compact
12px  component
16px  standard
24px  section
32px  page
48px  hero
```

Cards should generally use 12–20px inner padding.

---

# 71. Border Radius

Use modest radius.

Recommended:

```text
button       7–8px
input        8px
card         10–12px
drawer       12–16px
pill         999px
```

Avoid huge 24–32px "consumer app" cards.

---

# 72. Shadows

Minimal.

Preferred:

- almost none at rest
- small elevation for overlays
- stronger shadow only for drawers/modals

The black visual language should rely on contrast and geometry rather than shadows.

---

# 73. Cards

Cards should represent semantic objects.

Good:

- Incident
- Evidence
- Hypothesis
- Action
- Repair
- Service

Bad:

- arbitrary layout containers styled as floating cards

Use flat sections when appropriate.

---

# 74. Tables

Tables are the default for:

- incidents
- deployments
- evaluations
- audit logs
- tasks
- services

Cards are the default for:

- recommendations
- hypotheses
- evidence
- action proposals

---

# 75. Charts

Charts should answer an operational question.

Good:

```text
Did latency rise after deployment?
Did error rate recover after remediation?
```

Bad:

```text
Three colorful charts because dashboards need charts.
```

Use:

- line charts
- sparklines
- distribution views
- before/after comparison

Avoid donut charts except where genuinely useful.

---

# 76. Color Budget

Aegis is mostly monochrome.

Approximate visual ratio:

```text
95% black/white/gray
3% semantic status colors
2% accent
```

Accent use should be rare.

Default accent:

a restrained electric-blue/cyan family can be used for interactive intelligence states, but not as decorative brand wallpaper.

---

# 77. AI Visual Language

Avoid the standard:

```text
sparkles ✨
glow
robot icon
chat bubble everywhere
```

Aegis AI should look operational.

Use:

```text
Investigation
Verification
Evidence
Prediction
Confidence
Action
```

AI is represented through process and state, not magic.

---

# 78. Conversational AI Surface

Aegis can provide a chat interface, but it must not be the primary interface.

The preferred pattern:

```text
Structured investigation
        +
Ask Aegis
```

The chat should be contextual.

Example:

User:

> Why did you reject Redis?

Aegis:

> Redis p99 remained within its 4–7ms baseline during the incident. Three affected checkout traces also bypassed Redis latency entirely.

`[View evidence]`

The answer is anchored to the incident state.

---

# 79. "Ask Aegis" Context

Context is automatically inherited:

```text
incident
service
time range
current hypothesis
selected evidence
```

The user should not need to paste logs.

---

# 80. UX for Uncertainty

This is critical.

When evidence is insufficient:

```text
Aegis cannot establish a root cause yet.

Confidence: 41%

Known:
...
Missing:
...

Best next check:
...
```

Never display:

```text
Root cause: Unknown
Confidence: 87%
```

without explaining why.

---

# 81. UX for Contradictory Evidence

Show:

```text
CONFLICTING EVIDENCE

Git suggests:
deployment regression

Telemetry suggests:
dependency degradation

Aegis is investigating the disagreement.

[Compare evidence]
```

This is better than silently averaging signals.

---

# 82. UX for Provider Failure

Example:

```text
AI provider degraded

Primary reasoning provider unavailable.
Aegis switched to fallback model.

Behavior may differ from benchmark baseline.

[View provider status]
```

This should become part of the trace.

---

# 83. Audit UX

Audit records should be readable, not database dumps.

Example:

```text
10:38:21
Aegis proposed rollback

Reason:
verified deployment regression

Policy:
Tier 2

Approved by:
Platform Engineer

Executed:
10:39:04

Verified:
10:40:18
```

Click for raw event data.

---

# 84. Reliability "Why" Layer

Every important dashboard metric should support:

`Why?`

Example:

```text
MTTR ↑ 14%

Why?
- 2 recurring database incidents
- approval wait time +8m
- telemetry outage +4m
```

Aegis should not just visualize metrics.

It should explain them with evidence.

---

# 85. Design for the 2 AM Scenario

The most important design test is:

A sleep-deprived SRE opens Aegis after a page.

Within 10 seconds they should know:

```text
What broke
How bad
Where
Why Aegis thinks so
What it has already checked
What it recommends
Whether it needs me
```

Anything else is secondary.

---

# 86. Primary Interaction Flow

The golden path:

```text
Alert
 ↓
Incident
 ↓
Aegis investigation starts immediately
 ↓
Live progress
 ↓
Evidence
 ↓
Topology
 ↓
Hypotheses
 ↓
Code debugging
 ↓
Candidate repair
 ↓
Tests
 ↓
Staging
 ↓
Verification
 ↓
Risk decision
 ↓
Human approval OR safe autonomy
 ↓
Production
 ↓
Verification
 ↓
Resolution
 ↓
Memory
 ↓
Evaluation
```

Every stage must have an identifiable UI state.

---

# 87. Incident State → UI State Mapping

```text
RECEIVED
  show ingestion state

TRIAGING
  show impact + alert correlation

INVESTIGATING
  show live investigation strip

DIAGNOSING
  show hypothesis workspace

DEBUGGING
  show Debug Workbench entry

VERIFYING
  show verification center

AWAITING_APPROVAL
  show approval surface

REMEDIATING
  show action progress

MONITORING
  show before/after telemetry

RESOLVED
  show resolution summary + memory

ESCALATED
  show missing evidence / human handoff
```

---

# 88. Navigation Behavior

Sidebar:

- persistent on desktop
- collapsed on demand
- remembers preference

Active section should use:

- dark elevated background
- white text
- small semantic indicator

Do not use giant colored active bars.

---

# 89. Breadcrumbs

Examples:

```text
Incidents / INC-2847
Incidents / INC-2847 / Debug
Services / payment-service / Deployments / 91d4e1
Evaluation / Runs / INC-2847
```

Breadcrumbs are especially important in graph/code drilldowns.

---

# 90. Search UX

Global search should feel instantaneous.

Categories:

```text
Incidents
Services
Deployments
Code
Runbooks
People
Evidence
AI runs
```

Search results show:

- entity type
- title
- status
- context
- last updated

---

# 91. Keyboard UX

Suggested:

```text
⌘K       command palette
G I       incidents
G S       services
G E       evaluation
I         incident investigation
D         debug
A         approvals
R         reliability
Esc       close drawer
```

Do not overload common browser shortcuts.

---

# 92. UI Component Inventory

Core components:

```text
AppShell
Sidebar
CommandPalette
Breadcrumbs
StatusChip
SeverityBadge
MetricStrip
Sparkline
IncidentHeader
InvestigationStrip
EvidenceCard
EvidenceDrawer
HypothesisCard
HypothesisTree
CausalPath
ServiceGraph
Timeline
TimelineItem
AgentActivity
AgentRunDrawer
CodeWorkbench
DiffViewer
VerificationPanel
RiskPanel
ApprovalDrawer
TaskList
SystemHealth
EvaluationScorecard
EvaluationTable
AuditTimeline
EmptyState
ErrorState
Skeleton
Toast
Modal
Drawer
```

---

# 93. Component API Principle

Components should consume semantic data structures.

Bad:

```text
<Component red="#FF0000" type="x" />
```

Good:

```text
<SeverityBadge severity="P1" />
```

The design system owns semantic rendering.

---

# 94. Design Tokens

Example token set:

```css
--bg: #000000;
--surface-1: #050505;
--surface-2: #080808;
--surface-3: #0D0D0D;
--surface-4: #111111;

--text-primary: #F5F5F5;
--text-secondary: #A0A0A0;
--text-tertiary: #666666;

--border-subtle: #151515;
--border-default: #1C1C1C;
--border-strong: #292929;

--status-critical: #FF5F56;
--status-warning: #FFD166;
--status-info: #7DD3FC;
--status-success: #8DE8AE;
```

Exact values can be tuned during implementation.

---

# 95. Typography Tokens

```css
--font-sans: Inter, -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

--text-display: clamp(2.5rem, 5vw, 4.5rem);
--text-h1: 2.75rem;
--text-h2: 1.25rem;
--text-h3: 0.9rem;
--text-body: 0.82rem;
--text-meta: 0.68rem;
```

---

# 96. Copywriting Principles

Aegis copy should be:

- direct
- factual
- concise
- operational
- transparent

Avoid:

```text
Aegis thinks something magical is happening...
```

Use:

```text
Aegis found 5 supporting signals.
```

Avoid:

```text
We are absolutely certain...
```

Use:

```text
Evidence supports this hypothesis with 86% calibrated confidence.
```

---

# 97. AI Status Vocabulary

Preferred:

```text
Investigating
Analyzing
Correlating
Testing
Reproducing
Verifying
Waiting for approval
Executing
Monitoring
Escalated
```

Avoid:

```text
Thinking
Cooking
Dreaming
Magic
```

---

# 98. Notification Design

Notifications should contain the next useful action.

Bad:

> New incident detected.

Good:

> P1 incident INC-2847 is active. Aegis has isolated payment-service and is validating a fix. Review required in 3 min.

Mobile notifications should be short.

---

# 99. Slack / Chat Surface

Aegis must remain useful even when the user is in Slack.

Slack message structure:

```text
INC-2847 · P1

Checkout latency cascade

Aegis:
payment-service is the strongest suspected origin.

Evidence:
5 validated signals

Current action:
Staging verification

[Open incident]
[View evidence]
[Approve]
```

The web UI contains the full state.

---

# 100. Status Page UX

For external communication:

```text
Incident
Affected component
Customer impact
Current status
Last updated
```

AI-generated content should always be reviewable before external publishing.

---

# 101. Design System States

Every interactive component must have:

```text
default
hover
focus
active
disabled
loading
success
warning
error
```

For actions also:

```text
pending
approved
executing
verifying
failed
rolled back
```

---

# 102. Security UX

Permissions should be understandable.

For restricted data:

```text
Restricted telemetry

You do not have permission to view this source.

Aegis excluded it from reasoning for this incident.
```

Do not reveal data in tooltips just because a user cannot access the full source.

---

# 103. Privacy UX

Private incidents:

- lock indicator
- clear visibility state
- restricted sharing controls

When sharing:

```text
Internal only
Restricted
External
```

Never make visibility ambiguous.

---

# 104. AI Evaluation Visual Semantics

Use status colors sparingly.

Green:

verified/passing

Amber:

needs review

Red:

failed

Gray:

not evaluated

Do not turn every evaluation number into a traffic light.

---

# 105. "Proof" as a UI Primitive

Aegis should have a concept called:

**Proof**

A proof item contains:

```text
Claim
Evidence
Test
Result
Timestamp
```

Example:

```text
PROOF

Claim:
Connection-pool exhaustion caused checkout failures.

Observed:
98% pool utilization.

Test:
Reproduced same failure in staging.

Verification:
47/47 tests pass after patch.

Status:
PROVEN
```

This becomes a core Aegis design concept.

---

# 106. Why This Differentiates Aegis

incident.io has built a strong incident-response experience around ergonomics, Slack-native collaboration, timelines, service/catalog context and Investigations.

Aegis should not attempt to copy that product surface exactly.

Aegis's distinctive UI should be:

```text
Incident response
      +
Production graph
      +
Code debugging
      +
Execution verification
      +
Risk-aware autonomy
      +
AI evaluation
```

The central visual story is therefore:

**Signal → Cause → Code → Fix → Proof → Action**

---

# 107. Anti-Patterns

Do not build:

- generic dark dashboard templates
- giant metric cards everywhere
- rainbow dashboards
- giant glowing AI brain graphics
- chat-first UI
- one giant "AI answer" paragraph
- graph as decoration
- hidden evidence
- hidden risk policy
- modal-heavy workflows
- too many primary buttons
- tiny 7px unreadable text
- fake charts
- fake real-time AI indicators
- UI states that do not correspond to backend state

---

# 108. Visual QA Checklist

Before shipping a page:

### Hierarchy
Can the user identify the most important thing in 2 seconds?

### Density
Can an SRE scan it quickly without visual fatigue?

### Contrast
Is the page readable under low-light conditions?

### State
Is current status obvious?

### AI
Is it clear what AI did versus what the system measured?

### Evidence
Can every important claim be traced?

### Action
Is the consequence of every write action obvious?

### Motion
Does motion communicate state?

### Responsive
Does the core workflow survive a 375px viewport?

---

# 109. Primary Screen Priority

Build in this order:

```text
1. Incident Detail
2. Investigation Workspace
3. Debug Workbench
4. Service Graph
5. Approval / Risk
6. Home / Command Center
7. Service Detail
8. AI Evaluation
9. Reliability
10. Audit / Governance
```

Do not spend most of the initial UI effort on the marketing homepage.

The incident page is the product.

---

# 110. Implementation Guidance

Recommended frontend stack:

- Next.js
- TypeScript
- Tailwind CSS
- accessible headless primitives
- CSS variables for theme/tokens
- React Server Components where useful
- client components only where interaction requires them
- SSE/WebSocket for live incident state
- SVG/canvas/WebGL selectively for topology

For the graph:

Start with SVG for the first production-grade implementation.

Move to a more specialized graph rendering approach only when topology scale or interaction requirements justify it.

---

# 111. Frontend Data Strategy

Never make UI state the source of operational truth.

The backend owns:

- incident status
- evidence
- agent state
- approvals
- policy
- action status
- verification state

The frontend subscribes to:

```text
IncidentStateEvent
AgentActivityEvent
EvidenceEvent
ActionEvent
VerificationEvent
SystemHealthEvent
```

The UI must reconcile events idempotently.

---

# 112. Streaming UX

Streaming updates should be incremental.

Example:

```text
10:35:12
Evidence found

10:35:18
Topology updated

10:35:24
Hypothesis confidence changed

10:35:41
Staging reproduction started

10:36:10
Reproduction passed
```

Do not rerender the entire page for every event.

---

# 113. Performance Requirements

Target:

- initial shell <1.5s on normal connection
- incident first paint <1.5s
- live event rendering <100ms
- drawer open <150ms
- command palette <100ms perceived response
- graph initial render <1s for common service graphs
- virtualize long timelines
- paginate/virtualize evaluation tables

---

# 114. Design Testing

Test with at least:

### Scenario A
P1 incident at 2 AM.

### Scenario B
No telemetry source.

### Scenario C
AI confidence low.

### Scenario D
Dangerous remediation requiring approval.

### Scenario E
Safe remediation executes automatically.

### Scenario F
Candidate patch fails staging.

### Scenario G
Two simultaneous incidents.

### Scenario H
Large 100+ service graph.

### Scenario I
AI provider fallback.

### Scenario J
Unauthorized user accesses restricted evidence.

The UI must remain coherent in all ten.

---

# 115. Research Traceability

The design decisions in this document are informed by current incident.io product behavior documented in 2026.

Relevant sources:

1. Navigation changes and My Tasks:
https://incident.io/changelog/upcoming-navigation-changes

2. Investigations UX and end-to-end coding workflow:
https://incident.io/blog/how-it-feels-to-run-an-incident-with-ai-sre

3. AI SRE / Investigations model:
https://incident.io/blog/ai-sre-agent-definition
https://incident.io/blog/what-is-ai-sre-complete-guide-2026

4. Investigation dashboard trends, telemetry permissions and health:
https://incident.io/changelog/shard-alert-source-rate-limits

5. Alert timeline:
https://docs.incident.io/alerts/alert-timeline

6. Post-mortem AI workflow:
https://incident.io/changelog/post-mortems-upgrade

7. Current brand center, used only to understand their visual system and deliberately not copied:
https://incident.io/brand

These sources are references for product/UX research, not design assets.

---

# 116. Final Aegis UX Contract

The Aegis interface should make this experience feel natural:

```text
Something breaks.

Aegis already knows.

The responder opens the incident.

The answer is not a chatbot response.

It is a living operational model:

    impact
      ↓
    topology
      ↓
    evidence
      ↓
    hypotheses
      ↓
    code
      ↓
    reproduction
      ↓
    repair
      ↓
    verification
      ↓
    risk
      ↓
    action
      ↓
    proof

The human sees exactly where Aegis is confident,
where it is uncertain,
what it has verified,
and what still needs human judgment.
```

That is the core UX promise:

> **Aegis turns production complexity into a visible chain of evidence, decisions, and proof.**

The UI should feel as though the system has already done the hard work before the SRE arrives.

