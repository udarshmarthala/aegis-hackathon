# AWS architecture

Single region. No Kubernetes, no GPUs, no multi-account landing zone. The
product's contract is container-centric (ESD section 4.2) and Aegis should not
require a cluster orchestrator merely to run.

```
                      Vercel (frontend, outside AWS)
                                 |
                                 v
        +--------------------- AWS -----------------------------+
        |                                                       |
        |   ALB (public subnets, HTTPS)                         |
        |     |                                                 |
        |     v                                                 |
        |   ECS Fargate: aegis-<env>-api     (on-demand, 1-2+)  |
        |     |            |           \                        |
        |     |            |            \--> SQS investigations |
        |     |            |                       |            |
        |     |            |                       v            |
        |     |            |    ECS Fargate: aegis-<env>-worker |
        |     |            |        (FARGATE_SPOT, 0..N)        |
        |     |            |           |        |               |
        |     v            v           v        v               |
        |   RDS PostgreSQL 16     S3 artifacts  Neo4j (Fargate  |
        |   (system of record)    (evidence,    + EFS, private  |
        |        ^                 evaluations)  DNS only)      |
        |        |                                              |
        |   ElastiCache Redis (optional: SSE fan-out only)      |
        |                                                       |
        |   CloudWatch Logs + alarms + SNS + AWS Budgets        |
        +-------------------------------------------------------+
                                 |
                                 v
              External LLM APIs, LangSmith, GitHub, Slack
                       (egress via NAT Gateway)
```

Everything is created by `infra/terraform/`. See `docs/terraform.md` for the
module layout and `docs/cost-strategy.md` for why each size was chosen.

---

## Components

### Entry: ALB

Public subnets. HTTPS on 443 with `ELBSecurityPolicy-TLS13-1-2-2021-06` (TLS
1.2 minimum, TLS 1.3 capable) and port 80 redirecting to it with a permanent
301 — as soon as `certificate_arn` is set. Access logs go to a dedicated S3
bucket with its own lifecycle rule.

The ALB is deliberately internet-facing: it *is* the public API. The
Vercel-hosted frontend calls it, operators reach the incident UI through it,
and external alert sources POST to `/alerts/ingest`. Making it internal would
not harden Aegis, it would remove it. What guards the boundary is security
group ingress CIDRs, per-request Firebase ID token verification, the
`ALERT_INGEST_TOKEN` on the ingest route, `drop_invalid_header_fields` and
`desync_mitigation_mode = "defensive"` — not a private address.

**Without a certificate there is no listener.** `certificate_arn` is required
in production by a variable validation. In staging it may be empty, because
issuing an ACM certificate needs DNS ownership of a domain this project does
not yet hold — and in that state the ALB serves nothing at all unless
`allow_insecure_http` is also set to `true`. That flag defaults to `false`
everywhere and production hardcodes it at the module call, so it is not a
variable anyone can flip. An `aws_lb` precondition reports the no-listener
state at plan time instead of leaving an endpoint that silently refuses every
connection. `docs/security.md`, "TLS at the edge", has the full table.

Idle timeout is **300 seconds**, not the 60-second default. The incident stream
is Server-Sent Events and is legitimately quiet between events; a 60-second
timeout would drop an operator's live view of an investigation every minute.

Health check is `/health/ready`, not `/health/live`. A task whose database
connection has gone is alive and useless, and taking it out of rotation is the
point.

### Control plane: ECS Fargate API service

`aegis-<env>-api`. On-demand capacity only — interrupting a request-serving
task to save a few dollars an hour is a bad trade. Target-tracking autoscaling
on CPU.

The deployment circuit breaker is on with `rollback = true`, so a rollout that
never reaches a steady state is reverted by ECS without waiting for anyone to
notice a hanging CI job.

### Work distribution: SQS

`aegis-<env>-investigations` with a dead-letter queue after three delivery
attempts. Visibility timeout is 900 seconds, which must stay above the agent
supervisor's wall-clock budget (`AGENT_MAX_WALL_SECONDS`, default 600). Set it
below and a slow investigation is redelivered while the first is still running,
producing two agents and two sets of proposed actions for one incident.

The DLQ alarm fires at depth greater than zero over a single evaluation period.
One dead letter is a defect, not noise.

### Investigation: ECS Fargate worker service

`aegis-<env>-worker` on `FARGATE_SPOT`, scaling between `worker_min_count` and
`worker_max_count` on SQS queue depth.

Spot is safe here in a way it usually is not: an investigation job is a row in
Postgres taken with `FOR UPDATE SKIP LOCKED`, so a two-minute Spot interruption
notice releases the lease and another worker picks the job up. The interruption
costs latency, never work.

Scaling uses **step scaling**, not target tracking. Target tracking cannot
scale a service from zero: with no running tasks a backlog-per-task metric is
undefined and the policy has nothing to act on. An alarm on absolute queue
depth works from zero, which is the entire point.

### System of record: RDS PostgreSQL 16

`db.t4g.micro` in staging, `db.t4g.small` Multi-AZ in production. gp3 storage
with autoscaling to a ceiling, encrypted with a customer-managed KMS key, and
`rds.force_ssl = 1` so a non-TLS connection is refused by the server rather
than merely discouraged.

PostgreSQL 16 specifically, because migrations `005_memory_eval.sql` and
`006_retrieval.sql` run `CREATE EXTENSION IF NOT EXISTS vector`. pgvector is
available on RDS PostgreSQL 16 and creatable by the master user.

The master password is generated and owned by RDS
(`manage_master_user_password = true`). It never passes through Terraform, so
it never lands in state, in a plan artifact, or in a CI log. ECS reads it with
the JSON-key selector `<secret-arn>:password::`.

### Topology: Neo4j

One Fargate task with an EFS volume, reachable only through Cloud Map private
DNS at `neo4j.aegis-<env>.internal:7687`. Not internet-facing, not highly
available.

That is a deliberate match to the data's criticality. Neo4j holds topology, not
the system of record (CLAUDE.md invariant 10). Losing it degrades confidence
and records an evidence gap; nothing authoritative is lost. Paying for an HA
graph cluster to protect derived data while the authoritative Postgres runs
single-AZ would be spending in the wrong place.

The service uses `deployment_maximum_percent = 100` with
`minimum_healthy_percent = 0`: two Neo4j tasks writing to one EFS directory
would corrupt the store, so the old task stops before the new one starts.

Set `graph_deployment_mode = "external"` to point at Neo4j AuraDB instead, or
`"disabled"` to run without topology at all.

### Cache and SSE bus: ElastiCache Redis, optional

Off in staging, on in production. Redis is a cache and a pub/sub bus, never
authoritative, and the API degrades to direct reads when it is absent.

The one condition that makes it mandatory is **more than one API task**. SSE
events published by task A never reach a browser connected to task B without
the Redis fan-out, so half the operators watching an incident would see a
stream that silently stops updating. Both environment roots enforce the pairing
with a variable validation rather than leaving it to a comment.

### Artifacts: S3

One bucket, four prefixes, four lifecycle rules:

| Prefix | Standard-IA | Glacier IR | Expires |
|---|---|---|---|
| `investigations/` | 30d | 90d | 365d |
| `evidence/` | 30d | 120d | 730d |
| `executions/` | 30d | 90d | 365d |
| `evaluations/` | 60d | 180d | 1095d |

Evaluation artifacts outlive everything else, because a benchmark result you
cannot reproduce is not a benchmark result. Versioning is on, so overwriting a
cited piece of evidence is recoverable rather than a silent loss.

One bucket rather than four: four buckets means four policies, four sets of
block-public-access settings, and four chances to get one of them wrong.

**Encryption is SSE-KMS under the environment's customer-managed key**, not
SSE-S3. This bucket holds archived evidence and raw execution output — the
data that makes a past incident auditable — so it gets the two properties an
AWS-owned key cannot give: every decrypt recorded in CloudTrail against a
named principal, and revocation by disabling one key rather than by editing
IAM policies. The key already exists for RDS and Secrets Manager, so there is
no extra key to manage, and `bucket_key_enabled` keeps the KMS request charge
negligible under replay's read-heavy pattern.

The API and worker task roles carry `kms:Decrypt` and `kms:GenerateDataKey`
conditioned on `kms:ViaService = s3.<region>.amazonaws.com`. Without those
grants every artifact write fails closed with `AccessDenied` rather than
falling back to a weaker cipher. The ALB access log bucket stays on SSE-S3
because AWS log delivery writes it; `docs/security.md` records that decision
and its expiry.

### Observability

One CloudWatch log group per environment, `/aegis/<env>`, with stream prefixes
`api`, `worker`, `migrate` and `neo4j`. Retention is 30 days in staging and 90
in production. Never unlimited — the module refuses a zero value, because zero
means "keep forever".

Alarms cover the load balancer (ELB 5xx, target 5xx, unhealthy hosts, p95
latency), the database (CPU, free storage, connections), the services (CPU and
memory) and the queues (dead letters, oldest-message age). They publish to an
SNS topic; subscribe email and a chat webhook to it.

Aegis emits its own OpenTelemetry metrics and traces, which is why ECS
Container Insights is **disabled** by default: it would be a second, billed
copy of data the platform already collects.

---

## Network

Two availability zones — the minimum an ALB accepts. A third costs more NAT and
more interface endpoints for availability this deployment does not need.

| Tier | Contents | Reachable from |
|---|---|---|
| public | ALB, NAT Gateway | the internet |
| private | ECS tasks, RDS, ElastiCache, EFS mount targets | inside the VPC only |

Security groups reference each other rather than CIDR ranges. The database
group accepts 5432 from the task group and from nothing else; the task group
accepts 8000 from the ALB group and from nothing else. A subnet renumbering
cannot silently widen any of it.

Task egress is unrestricted, deliberately. Aegis must reach external LLM
providers, LangSmith, GitHub and Slack, none of which publish a stable address
range. An egress allowlist here would be an IP list that silently breaks the
product every time a provider changes an address; the control lives at the
application layer, in the configured set of base URLs.

S3 and DynamoDB **gateway** endpoints are always created. They are free, and
the S3 one matters most: ECR image layers are served from S3, so without it
every task start pays NAT data-processing charges for the whole image.

Interface endpoints are off by default. See `docs/cost-strategy.md` — at this
volume five of them across two AZs cost more than the NAT Gateway they would
offset.

---

## What runs where

| Concern | Where | Why not somewhere else |
|---|---|---|
| Frontend | Vercel | Next.js App Router on its native platform; no ALB, no ECS task, no cost in this account |
| API | ECS Fargate | container-centric by contract; Lambda cannot hold an SSE connection for the length of an investigation |
| Workers | ECS Fargate Spot | investigations run for minutes and exceed Lambda's ceiling; Spot is safe because jobs are leased from Postgres |
| Job state | Postgres | "create the incident and schedule its investigation" stays in one transaction |
| Queue | SQS | autoscaling needs a queue-depth metric CloudWatch can see |
| Topology | Neo4j on Fargate | the graph is small and non-authoritative; a managed cluster is not warranted |
| Secrets | Secrets Manager | native ECS resolution before container start, so the application never holds permission to read its own credentials |

---

## Known gaps

These are real. Do not read past them.

### 1. The worker consumes from Postgres, not SQS

`backend/src/aegis/persistence/jobs.py` implements a durable job queue on
Postgres with `FOR UPDATE SKIP LOCKED`, and `aegis.worker.main` polls it. The
backend has no SQS producer or consumer today.

The queue, the dead-letter queue, the IAM permissions and the queue-depth
scaling policies are all provisioned and correct, but until the backend
publishes to SQS the queue stays empty, the backlog alarm never fires, and a
service with `worker_min_count = 0` never scales up. Investigations would still
be picked up — but only by a worker that is already running.

**Until the SQS driver lands**, set in the environment root:

```hcl
worker_scaling_mode = "cpu"
worker_min_count    = 1
```

That runs one always-on worker polling Postgres and scales on CPU. The cost is
roughly $15/month on Spot. The compute module publishes `AEGIS_QUEUE_URL` to
both services already, so the SQS driver needs no infrastructure change.

### 2. Firebase credentials are a file, and Fargate has no file-shaped secrets

`backend/src/aegis/api/security.py` loads Firebase credentials from
`FIREBASE_SERVICE_ACCOUNT_PATH`, a filesystem path. ECS can inject a secret as
an environment variable but not as a file.

The compute module bridges this: when `firebase_secret_arn` is set, the
container command is wrapped in a shell that writes the injected JSON to
`/tmp/firebase-service-account.json` under `umask 077` and then `exec`s the
application, so SIGTERM still reaches it for a graceful drain. The value is
never echoed and the variable is unset before the application starts.

It works, and it is a bridge. The durable fix is for the backend to accept the
service account as inline JSON. See `docs/security.md`.

### 3. No integration tests exist yet

`backend/tests/integration/` is empty. `ci-backend.yml` stands up real
Postgres, Redis and Neo4j containers, applies the migrations with the
application's own runner, and asserts that re-running them is a no-op — so the
schema path is genuinely exercised. But `pytest -m integration` currently
collects nothing, and the job emits a GitHub warning saying so rather than
reporting a green test run that tested nothing.

### 4. Nothing here has been applied to AWS

`terraform validate` passes for `bootstrap`, `environments/staging` and
`environments/production`. No `plan` has ever run against a real account, so
quota limits, ACM certificate availability, service-linked role creation and
region-specific instance availability are all unverified.

Three security decisions inherit that gap and cannot be closed from here:

* **HTTPS is configured, not proven.** The listener, the TLS 1.2-minimum
  policy and the 301 redirect exist in code and validate, but no certificate
  has been issued, because ACM validation requires DNS ownership of the API
  domain. Until someone owns that domain and sets `certificate_arn`, every
  environment is in the no-listener or explicitly-insecure state.
* **The artifacts CMK grants are unexercised.** The `kms:ViaService` condition
  on the API and worker roles is the kind of thing that is either exactly
  right or silently denies every write; only a real apply tells you which.
* **SSE-KMS for the ALB log bucket stays open.** Pointing AWS log delivery at
  a customer-managed key needs a key policy that cannot be validated without
  an account, and whose failure mode is access logs quietly not being written.
  Recorded in `.trivyignore` with an expiry rather than fixed blind.

### 5. The frontend image is built but not deployed by this infrastructure

`docker.yml` builds and pushes `aegis/web`, and the compose stack uses it. The
target architecture puts the frontend on Vercel, so no ECS service runs it. The
image exists for local parity and as a fallback if Vercel is ever dropped.
