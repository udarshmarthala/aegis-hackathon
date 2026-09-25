# AWS hackathon deployment: backend only

This is the least-cost AWS deployment of the Aegis **backend** that still serves
the demo well. The Next.js frontend stays on Vercel. The Terraform is
`infra/terraform/environments/hackathon/` and the pipeline is
`.github/workflows/deploy-hackathon.yml`. `docs/AWS_COST_ANALYSIS.md` prices
each line.

The staging and production designs (`docs/aws-architecture.md`) are unchanged.
This document records where the hackathon root diverges from them and why.

**Status (2026-09-26):** `terraform validate` and a read-only `terraform plan`
(68 resources to add, `-var-file=hackathon.tfvars`, saved as the gitignored `tfplan`) pass against account 822491881190 in us-west-2. The
backend image builds for `linux/arm64`, and every Python dependency installs
from a prebuilt aarch64 wheel. **Nothing has been applied.**

---

## 1. The decision in one paragraph

The deployment has one API task and one worker task on ARM64 Fargate. The API
runs on-demand; the worker runs on Spot and can be switched to on-demand. Both
tasks run in public subnets. Each has a public IPv4 address and a security group
that accepts traffic only from the load balancer. There is no NAT Gateway. The
system of record is a single-AZ `db.t4g.micro` RDS PostgreSQL 16 in private
subnets. The public HTTPS entry point is a CloudFront distribution on its free
`*.cloudfront.net` certificate. It reaches an **internal** ALB through a
CloudFront VPC origin. The work queue stays the existing Postgres `workflow_jobs`
queue with `FOR UPDATE SKIP LOCKED`, and there is no SQS. Redis and Neo4j are
not deployed; both degrade by design. The deployment runs in **observe and
recommend** mode. Aegis ingests alerts, investigates them with Claude on
Bedrock, and proposes and gates actions, but it has no write target in AWS. The
estimated fixed cost is **about $60/month (≈$2/day)**, or about $70/month with
an on-demand worker.

---

## 2. Diagram

```
   Browser (Vercel-hosted Next.js app, https://<app>.vercel.app)
        |  HTTPS, Authorization: Bearer <Firebase ID token>, SSE via fetch()
        v
+------------------------------ AWS us-west-2 ------------------------------+
|                                                                           |
|  CloudFront  (d123.cloudfront.net, default cert, CachingDisabled,         |
|              AllViewer headers, origin read timeout 60 s, no compression) |
|        |  VPC origin: AWS private network, HTTP :80                        |
|        v                                                                  |
|  +--------------------------- VPC 10.60.0.0/20 -----------------------+   |
|  |                                                                    |   |
|  |  private subnets (2 AZ)            public subnets (2 AZ, IGW)       |   |
|  |  +---------------------+           +----------------------------+  |   |
|  |  | internal ALB        |  :8000    | API task  (0.5 vCPU/1 GB,  |  |   |
|  |  | SG: CF prefix list  |---------->| ARM64, on-demand, pub IP)  |--+---+--> Internet egress
|  |  | /metrics -> 404     |           +----------------------------+  |   |    (Gemini, RawTree,
|  |  +---------------------+           | worker task (0.5/1 GB,     |--+---+-->  Nimble, BFL,
|  |                                    | ARM64, Spot, pub IP)       |  |   |     Firebase certs)
|  |  +---------------------+  :5432    |  - horizon step loop       |  |   |
|  |  | RDS PostgreSQL 16   |<----------|  - Postgres job poller     |  |   |
|  |  | db.t4g.micro, gp3   |<----------|  - heartbeat + forwarder   |  |   |
|  |  | single-AZ, TLS-only |           +----------------------------+  |   |
|  |  +---------------------+                     |                     |   |
|  |                                              | task role (no keys)  |   |
|  |  S3 + DynamoDB gateway endpoints (free)      v                     |   |
|  +----------------------------------------- Bedrock (us.* profiles) -+   |
|                                                us-east-1/2, us-west-2    |
|  ECR (arm64 image, immutable SHA tags)   Secrets Manager (2 secrets)      |
|  S3 artifacts bucket (SSE-S3, private)   CloudWatch Logs (7 days)         |
|  AWS Budget (tag-filtered)                                                |
+---------------------------------------------------------------------------+
        ^
        | OIDC (environment-scoped), push image / run migrate / roll services
   GitHub Actions: deploy-hackathon.yml (workflow_dispatch)
```

---

## 3. Components and why each was chosen

### 3.1 Compute: ECS Fargate on ARM64, with one API task and one worker task

| | API | Worker | Migrate (one-off) |
|---|---|---|---|
| Size | 0.5 vCPU / 1 GB | 0.5 vCPU / 1 GB | 0.25 vCPU / 0.5 GB |
| Capacity | FARGATE (on-demand) | FARGATE_SPOT (`worker_use_spot`) | FARGATE |
| Count | 1 (validation refuses >1: no Redis) | 1 | per deploy |
| Deploy | 200% max / 100% min: zero-downtime | 100% max / 0% min: never two heartbeats | run to completion |

- **ARM64.** Fargate ARM64 costs $0.03238 per vCPU-hour against $0.04048 for
  x86, which is 20% less. The switch is safe: a local `docker buildx --platform
  linux/arm64` build of `backend/Dockerfile` completed, and all dependencies
  installed from aarch64 wheels, including asyncpg, orjson, pydantic-core,
  grpcio, cryptography, uvloop and tiktoken. Only `aegis` itself was built from
  source. The image imported `aegis.api.app` and `aegis.worker.main` under
  arm64. The workflow builds on GitHub's native `ubuntu-24.04-arm` runners,
  which are free for public repositories. It also refuses to deploy if the
  image platform does not match the task definition's `cpuArchitecture`.
- **The API runs on-demand.** It holds every operator's SSE stream. Moving it to
  Spot would save about $10/month at the cost of dropped streams.
- **The worker runs on Spot, which is safe for correctness but not for
  latency.** Jobs are Postgres rows and every horizon step is checkpointed, so an
  interruption loses no work. However, the worker id is the task hostname, and
  a replacement Fargate task gets a new hostname. `reclaim_own()` therefore
  never matches, and the orphaned job waits for `reap_stale()` at 900 s. Switch
  to on-demand for the judging window (`worker_use_spot = false`, about +$9/month),
  or make app change A3.
- **0.5 vCPU is enough.** The worker waits on model calls and is not CPU-bound.
  1 GB leaves headroom over the roughly 400 MB resident Python process.
- The **sandbox is off** (`SANDBOX_ENABLED=false`) because Fargate has no Docker
  daemon. With it off, the sandbox tool reports that it is not configured,
  instead of failing on every call.

### 3.2 Ingress: CloudFront in front of an internal ALB through a VPC origin

The Vercel page is HTTPS, so the browser will not call a plain-HTTP API (mixed
content). HTTPS therefore needs a trusted certificate, and a trusted certificate
needs a domain name. This project does not own one.

| Option | ≈ $/month | SSE (long-lived) | HTTPS without a domain | Verdict |
|---|---|---|---|---|
| **CloudFront → internal ALB (VPC origin)** | **~17.6** (ALB only; CloudFront $0 in free tier; VPC origin free) | Yes: the 60 s response timeout applies **between packets**, and both SSE endpoints heartbeat every ≤15 s | Yes (`*.cloudfront.net`) | **Chosen** |
| Public ALB + ACM | ~24.9 (ALB 17.6 + 2 public IPv4 7.3) | Yes (idle timeout 300 s) | **No**: ACM needs a domain | Rejected until a domain exists |
| CloudFront → public ALB (HTTP origin) | ~24.9 | Yes | Yes | Rejected: Firebase tokens would cross the internet in plaintext between the edge and the ALB |
| API Gateway HTTP API + VPC Link + Cloud Map (no ALB) | ~1–3 | **No**: 30 s max integration timeout, response buffered | Yes | Rejected: SSE breaks |
| App Runner | ~25–30 for the API alone | 120 s request timeout cuts every stream | Yes | Rejected: SSE is cut, the worker cannot run there, and it needs a VPC connector for RDS |
| Lightsail containers | 10–15 + Lightsail DB 15 | Unverified | Yes | Rejected: no IAM task role, so Bedrock would need static keys, and RDS is reachable only by peering |
| One EC2 `t4g.medium` + docker compose (+ CloudFront VPC origin to the instance) | ~31 | Yes | Yes | Rejected as the primary design (see 3.9); kept as Plan B for a live fault-injection demo |
| NLB instead of ALB | same hourly rate | Yes | via CloudFront | No saving, and it loses HTTP health checks and the `/metrics` rule |

CloudFront details that matter:

- `CachingDisabled` together with the `AllViewer` origin request policy.
  CloudFront strips `Authorization` by default, and an origin request policy
  cannot list it on its own. Forwarding all viewer headers is the documented way
  to pass it with caching off. The same policy passes `Last-Event-ID`, `Origin`
  (for CORS) and `X-Aegis-Ingest-Token`.
- Compression is off, so the edge never buffers `text/event-stream` in order to
  compress it.
- The origin read timeout is 60 s, the maximum without a quota increase. The
  shortest heartbeat on the path is 15 s. (`stream.py` pings every 15 s, and
  `war_room_events.py` sends health every 5 s and a ping every 15 s.)
- **SSE through CloudFront is the one property here that has not been observed
  running.** After the first deploy, verify it by hand (§8, step 8).
- The default certificate's minimum TLS version is fixed by AWS. A custom domain
  with an ACM certificate in us-east-1 would let you pin `TLSv1.2_2021`.

### 3.3 Network: no NAT Gateway

| | $/month | Egress for Gemini / RawTree / Nimble / BFL / Firebase | Exposure |
|---|---|---|---|
| NAT Gateway (single) | 32.85 + $0.045/GB | yes | tasks unaddressable |
| **Public IPv4 per task (chosen)** | **7.30** (2 × $0.005/h) | yes | ENIs are addressable; the SG admits only the ALB SG on :8000 |
| VPC interface endpoints only | ~7.30 per endpoint per AZ | **no**: PrivateLink cannot reach SaaS APIs | – |

Outbound internet is a functional requirement, because RawTree, Nimble, BFL,
Gemini and Firebase's key endpoints are all public SaaS. For this environment
the cheapest correct choice is to give each task a public IP. This uses the
existing `modules/network` with `nat_strategy = "none_public"`, the option that
module documents as "acceptable for a cost-constrained staging environment,
never the default". The residual risk is the one `docs/cost-strategy.md`
states: a future security group mistake would expose the tasks to the internet.
Mitigations:

- The task SG has one ingress rule, from the ALB SG on 8000.
- RDS stays in private subnets and accepts 5432 only from the task SG.
- The ALB is internal, and its only ingress is the CloudFront origin-facing
  prefix list. Because the ALB has no public address, only a CloudFront VPC
  origin created in this account can use that rule.

S3 and DynamoDB **gateway** endpoints are created as usual. They are free, and
they keep ECR layer pulls off the public path.

### 3.4 Queue: keep Postgres `workflow_jobs`; no SQS

The brief asked for "PostgreSQL = state, SQS = work notification, ack / retry /
visibility / idempotency / DLQ, workers on Spot scaling toward zero." The
Postgres queue already provides each of those semantics:

| Semantic | `persistence/jobs.py` today | What SQS would add |
|---|---|---|
| Atomic enqueue | same transaction as the incident row | a dual write: commit, then publish. A lost publish needs an outbox sweeper, which is a poller |
| Exactly-once claim | `FOR UPDATE SKIP LOCKED` in one statement | at-least-once delivery, which still needs the Postgres claim to dedupe |
| Idempotency | partial unique index on (incident, kind) | nothing new |
| Retry / backoff | `fail()` → `run_after = now() + 30 s` | redrive |
| Visibility timeout | `locked_at` + `reap_stale(900 s)` | 900 s, which must stay above `AGENT_MAX_WALL_SECONDS` |
| Dead letter | `status = 'failed'` after `max_attempts`, visible in SQL | a DLQ plus an alarm |
| **Scale to zero** | no | **yes: this is the only real addition** |

The one thing SQS adds is worth very little here:

- **The money is small.** A Spot worker is about $5/month plus $3.65 for its
  IPv4 address. Scaling it to zero saves at most about $8.65/month, or $17.70
  with an on-demand worker.
- **The worker also hosts the heartbeat and the metrics forwarder**
  (`horizon_runtime.py`), which must run continuously. To scale the worker to
  zero, they would move to a separate always-on task (0.25 vCPU / 0.5 GB ARM,
  $7.21, plus $3.65 IPv4, about $10.86 in total). That costs more than the
  worker it lets you remove. The alternative is to move them into the API
  process, which is an app change, and then the API task becomes a second
  control loop.
- **The demo pays for scale to zero in latency.** The first alert after an idle
  period waits one 60 s alarm period plus a 30–60 s task start, about 90–120 s,
  before the investigation begins.
- **Scale-in would kill running investigations.** While an investigation runs,
  its message is invisible, so `ApproximateNumberOfMessagesVisible` reads 0 and
  the existing step-scaling policy would scale the worker in mid-investigation.
  Preventing that needs ECS task scale-in protection, which is more code.

**Decision:** one always-on worker polling Postgres. No SQS queue is created, so
nothing idle sits in the account looking like it works (the "inert queue" known
limitation in CLAUDE.md does not apply to this root). Revisit when there are
more than about three workers or the worker bill passes about $50/month. At
that point the design is: publish after commit, with Postgres as the source of
truth; a message carries only the `job_id`; the consumer does the same `claim()`
filtered by id; visibility is 900 s; the DLQ triggers after 3 receives; task
scale-in protection is set while a job runs; and the heartbeat moves to its own
task.

### 3.5 Postgres: RDS PostgreSQL 16, `db.t4g.micro`, single-AZ

- Graviton micro costs $0.016/h ($11.68/month) plus 20 GB gp3 at $0.115/GB-month.
  pgvector and pgcrypto (migrations 005, 006 and 010) are supported on RDS
  PostgreSQL 16.
- `engine_version = "16"` (major only). The other roots pin `16.4`, and **16.4 is
  no longer orderable in us-west-2**; 16.9 through 16.15 are (checked
  2026-09-26). That pin will fail `apply` for staging and production too.
- The master password is RDS-managed (`manage_master_user_password`), so it
  never enters Terraform state. The parameter group sets `rds.force_ssl = 1`.
  Performance Insights uses the free 7-day tier. There is no Enhanced Monitoring.
- **No `enabled_cloudwatch_logs_exports`.** RDS would create
  `/aws/rds/instance/.../postgresql` with no retention, which is the unbounded
  log bill this repository refuses elsewhere. `modules/datastores` has that
  defect.
- Single-AZ, with no deletion protection and no final snapshot by default,
  because this environment is meant to be destroyed after the event. Set
  `db_skip_final_snapshot = false` if the demo data should outlive it.

### 3.6 Redis: not deployed

`api_desired_count` is fixed at one or zero by a variable validation.
Without Redis:

- **War room** (`/v1/war-room/stream`): falls back to degraded mode. It replays
  from `horizon_events` in Postgres and takes a snapshot every 5 s. Events arrive
  up to 5 s late, and none are lost.
- **Incident stream** (`/v1/incidents/{id}/stream`): sends a snapshot every 15 s.
- **Kill-worker button**: unavailable. It needs the Redis control channel, and it
  is refused outside `AEGIS_ENV=local` anyway.
- **`RedisCachePort`**: absent. The cache action type has no port, which does
  not matter because the write path is closed (§5).

ElastiCache `cache.t4g.micro` would cost about $11.68/month to make the war room
push-driven instead of 5 s polling, which is not worth it for a demo.

### 3.7 Neo4j: not deployed (optional AuraDB Free)

Topology is a projection and is not authoritative (CLAUDE.md invariant 10).
Without it, graph tools report "source unavailable" and investigations record
an evidence gap. Set `neo4j_uri` to an AuraDB Free `neo4j+s://` URI and add
`NEO4J_PASSWORD` to the secret key lists to enable topology for $0.
`docs/cost-strategy.md` records the Free tier's node cap. Self-hosting Neo4j on
Fargate plus EFS (about $23/month) is not worth it here.

### 3.8 Storage, secrets, logs, registry

- **S3 artifacts bucket:** SSE-S3, public access blocked, `BucketOwnerEnforced`,
  TLS-only policy, versioning with 7-day noncurrent expiry, 90-day object
  expiry, and 1-day abort of incomplete uploads. **The backend does not write to
  S3 today.** Evidence and FLUX images live in Postgres. The bucket and the
  prefix-scoped task-role grants are there so the archive writer (A9) needs no
  infrastructure change. The customer-managed KMS key used in staging and
  production is replaced by SSE-S3. That saves $1/month and an untested
  `kms:ViaService` grant, and gives up per-principal decrypt audit and
  revocation by key. This is a recorded divergence.
- **Secrets Manager: two secrets, $0.80/month.**
  - `aegis-hackathon/app`: one JSON object. ECS selects each key with
    `<arn>:<KEY>::`. The secret-per-key layout costs $0.40 per key.
    Fail-closed behaviour is kept: ECS refuses to start a task whose selected
    key is missing.
  - The RDS-managed master secret.
  - Both use `aws/secretsmanager`, so the execution role needs no
    `kms:Decrypt`. Only the execution role can read them; task roles have no
    `secretsmanager:*` permission.
- **CloudWatch Logs:** `/aegis/hackathon` with 7-day retention and streams
  `api/`, `worker/` and `migrate/`. Container Insights is off.
- **ECR:** `aegis-hackathon/backend` with immutable tags, basic scan on push,
  expiry of untagged images after 1 day, and 10 tagged images kept. The
  repository is separate from the bootstrap's shared `aegis/backend`, so
  destroying this root touches nothing a future staging environment would share.

### 3.9 Why not a single EC2 box (the cheapest option)

A `t4g.medium` running the whole compose stack costs about $31/month: $24.53 for
the instance, $2.40 for 30 GB gp3, $3.65 for IPv4, and CloudFront at $0. It
includes Postgres, Redis, Neo4j and the demo workload, and **it is the only AWS
shape where the full INC-043 fault-injection demo runs**, because it has a
Docker socket for the Compose runtime adapter. It was rejected as the primary
design for three reasons:

- One instance is a single point of failure for the system of record.
- Postgres would be self-managed, with no PITR and a manual backup story.
- The Docker socket would be mounted into internet-facing containers on the
  same host that holds the system of record.

Keep it as Plan B if the live fault injection must run in the cloud rather than
on a laptop.

### 3.10 Modules reused, and why the others were not

| Module | Used? | Reason |
|---|---|---|
| `network` | **yes, as-is** | `nat_strategy = "none_public"` and `allowed_ingress_cidrs = []` give exactly this shape |
| `compute` | no | hardcodes an internet-facing ALB (2 billed IPv4 addresses), an HTTPS-or-insecure listener precondition, SQS-based scaling modes and an x86 default |
| `iam` | no | requires `queue_arn` / `dlq_arn`, has no Bedrock grant, and its deploy role is a broad Terraform-apply role; this workflow never runs Terraform |
| `datastores` | no | requires a CMK, exports RDS logs into an unbounded log group, pins an unorderable `16.4`, and always creates an ALB log bucket |
| `queue`, `graph` | no | no SQS, no Neo4j |
| `observability` | no | ALB/SQS alarms at $0.10 each, and its budget filter is broken (§9) |

---

## 4. Security, IAM and secrets

### 4.1 Boundary

| Hop | Control |
|---|---|
| Browser → CloudFront | HTTPS only (`viewer_protocol_policy = https-only`); CORS allows only the exact Vercel origin, and the production validator refuses wildcards |
| CloudFront → ALB | AWS private network; the ALB is internal; its SG admits only the CloudFront origin-facing prefix list on :80 |
| ALB → API | the task SG admits only the ALB SG on :8000; `/metrics` is answered 404 at the listener |
| API → RDS | private subnets; the data SG admits only the task SG on :5432; `rds.force_ssl = 1` |
| Tasks → internet | egress open (SaaS has no stable ranges); the application-layer allowlist is the configured base URLs |

### 4.2 Authentication and authorisation

- `AEGIS_ENV=production` turns on `Settings._production_hardening`. The process
  refuses to start if `AUTH_DEV_MODE` is true, if CORS contains a wildcard, or
  if any of `ALERT_INGEST_TOKEN`, `FIREBASE_PROJECT_ID` or `POSTGRES_PASSWORD` is
  missing. `_llm_configured` also demands a `GOOGLE_API_KEY` (see A1). The
  `aegis_env = "staging"` variable is the fallback.
- **Firebase:**
  - The service-account JSON is stored under `FIREBASE_SERVICE_ACCOUNT_JSON` in
    the app secret. The container command writes it to
    `/tmp/firebase-service-account.json` with umask 077, unsets the variable,
    and `exec`s uvicorn. This is the same bridge as in `modules/compute`, and A2
    removes it.
  - Roles come only from the `aegis_roles` custom claim. A Google sign-in
    without that claim is a **viewer**.
- **Alert ingestion** (`POST /v1/alerts`) requires `X-Aegis-Ingest-Token`, which
  is compared in constant time.

### 4.3 IAM roles

| Role | Can | Cannot |
|---|---|---|
| `aegis-hackathon-ecs-execution` | pull images; read **two named** secrets; write logs | anything else; never assumed by app code |
| `aegis-hackathon-api-task` | S3 `investigations/ evidence/ evaluations/` get/put/list; ECS Exec channels | Bedrock, delete, secrets, ECS writes |
| `aegis-hackathon-worker-task` | `bedrock:InvokeModel*` on **2 inference profiles**, and on their foundation models in us-east-1, us-east-2 and us-west-2 **only via those profiles** (`bedrock:InferenceProfileArn` condition); S3 prefixes incl. `executions/`; ECS Exec | any `ecs:UpdateService/StopTask`, delete, secrets |
| `aegis-hackathon-migrate-task` | nothing | everything |
| `aegis-hackathon-github-deploy` | ECR push to one repository; register task definitions (region-scoped); `RunTask` on the `-migrate` family in this cluster only; `Describe/UpdateService` on the 2 services; `PassRole` on the 4 roles to `ecs-tasks` only; read the migrate logs | Terraform, IAM changes, other repositories and services, secrets |

- Every `ecs-tasks` trust policy has `aws:SourceAccount` and `aws:SourceArn`
  conditions.
- **The GitHub trust uses `StringEquals` on exactly one subject:**
  `repo:udarshmarthala/aegis-hackathon:environment:aegis-hackathon`. GitHub
  issues that subject only after the Environment's protection rules pass.
- The account has **no GitHub OIDC provider** today, so this root creates one.
  It is an account singleton: if `infra/terraform/bootstrap` is applied later,
  set `create_github_oidc_provider = false` there or here.
- **Bedrock uses no keys.** `agents/brain/bedrock.py::_get_client` passes
  `aws_access_key`/`aws_secret_key` only when both are set, and `aws_profile`
  only when it is non-empty. Otherwise `AsyncAnthropicBedrock` falls through to
  botocore's default chain, which on Fargate is the task role. The task
  definition sets `AWS_PROFILE=""` and injects no AWS keys.

### 4.4 The write path is closed three independent ways

1. `AUTONOMY_ENABLED=false`, so the policy engine requires a human for every write.
2. There is no runtime target. `WORKLOAD_ADAPTER=ecs` with `ECS_CLUSTER=""`
   makes the ECS adapter report `ecs_cluster is not set`. An approved action
   then fails closed at execution ("no runtime adapter is configured; the action
   was not attempted"). The sandbox is off.
3. The worker's IAM role has no ECS or other write permission.

The gate chain still runs end to end (schema → evidence → policy → authz →
lease). Proposals, approvals and audit rows are real, and only the final
execute step has nothing to act on.

---

## 5. What works on AWS compared with local

| Capability | Local (compose) | AWS hackathon |
|---|---|---|
| API, Firebase auth, RBAC by claim | ✓ | ✓ |
| Alert ingest → incident → job (same transaction) | ✓ | ✓ |
| Horizon step loop, Bedrock brain (Sonnet 5 → 4.6 fallback on 403) | ✓ (profile creds) | ✓ (task role) |
| Gemini compactor, RawTree memory/MCP, Nimble, FLUX map | ✓ | ✓ (egress via task public IP) |
| Evidence store, citation validator, diagnoses, abstention | ✓ | ✓ |
| Proposals, gate chain, approvals, audit, incident memory | ✓ | ✓ (execution stops at "no runtime") |
| War room | live push (Redis) | **5 s polling** from Postgres |
| Incident SSE | push | **15 s snapshots** |
| Topology / GraphRAG | Neo4j | **evidence gap** (or AuraDB Free) |
| Prometheus / Tempo / Loki evidence, health cards | ✓ | **"source unavailable"** / cards unavailable |
| Heartbeat anomaly detection | scrapes workload | **idle**: nothing to scrape (`WORKLOAD_METRICS_TARGETS=""`) |
| Fault injection / reset / kill-worker | ✓ | **✗** (`_require_local`: AEGIS_ENV must be `local`) |
| Runtime writes (restart / scale / rollback) | Compose adapter via Docker socket | **✗** no Docker; ECS adapter has no target (see below) |
| Sandbox (patch tests) | Docker | **✗** (`SANDBOX_ENABLED=false`) |
| Evaluation catalogue page | ✓ | **"catalogue unavailable"**: `eval/scenarios` is outside the image's build context |
| GitHub / Slack | unconfigured in both | unconfigured |

**How to drive the demo on AWS:**

- Post an alert. The worker then runs a real investigation with the live Bedrock
  brain:

  ```bash
  curl -sS -X POST "$API/v1/alerts" -H "X-Aegis-Ingest-Token: $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"external_id":"demo-001","title":"checkout p99 latency > 2s","severity":"P2",
         "environment":"aws-hackathon","service_hint":"checkout"}'
  ```

- Or seed the prepared horizon data from inside the worker:

  ```bash
  aws ecs execute-command --cluster aegis-hackathon-cluster --task <id> \
    --container worker --interactive --command "python ..."
  ```

  `scripts/` is not in the image, so the seed scripts need to be copied in, or
  the seeding needs to run from a laptop against the database through ECS Exec.

- Run the full INC-043 fault-injection loop, including the Docker runtime and
  rollback, on the laptop compose stack, or on the Plan B EC2 host (§3.9).

**On the runtime adapter.** `integrations/runtime.py::EcsAdapter` is implemented:
it has the reads plus `StopTask` and `UpdateService` writes through boto3. It
still cannot run the demo on AWS for two reasons:

1. `rollback_deployment` maps a version to a **task-definition revision number**
   (`family:<version>`). The demo's versions are semver image tags (`1.4.1` and
   `1.4.2`), so the target would be invalid.
2. The workload (gateway, checkout, payment) would need its own Fargate services.
   That is 3 × (0.25 vCPU / 0.5 GB ARM, $7.21) plus 3 × $3.65 IPv4, about
   $33/month. It also needs Cloud Map DNS so the forwarder can scrape `/metrics`,
   and the worker would need ECS write IAM on that cluster.

App changes A7 and A8 list the work. Observe-and-recommend mode is the right
scope for this deployment.

---

## 6. CI/CD

`.github/workflows/deploy-hackathon.yml` runs on `workflow_dispatch` only.

| Job | Does |
|---|---|
| `build and push image` | preflight on variables; check the platform against the task definition; skip if the SHA tag already exists (immutable); buildx on a native arm64 runner; **trivy HIGH/CRITICAL gate**; push `:<sha>` |
| `run migrations` | registers the next `-migrate` revision from the latest one with only the image changed; `run-task` with the API service's own network configuration (public IP included, which is required with no NAT); waits; prints logs; fails on a non-zero exit |
| `roll out services` | prints the rollback commands first; registers the API and worker revisions; `update-service` (plus `--desired-count 1` when `bring_up`); `services-stable`; **asserts the new revision is the one running**, because a circuit-breaker rollback also ends "stable" |
| `health check` | `scripts/ci/smoke-test.sh` through CloudFront (live, ready, environment identity, anonymous read → 401/403, untokened ingest refused); `/metrics` → 404 |

Terraform owns task **definitions**, and the workflow owns the **running
revision**; the services ignore `task_definition`. A Terraform config change
(for example a new environment variable) registers a new revision, which the
next workflow run picks up because it copies the *latest* revision. The existing
required checks ("pytest (unit)", "ruff + mypy", "lint + types + build",
"gitleaks") are untouched. The workflow deliberately uses `HACKATHON_*`
variables, because setting `AWS_DEPLOY_ROLE_ARN` would arm `docker.yml` and
`deploy-staging.yml`.

---

## 7. App-code changes needed for AWS (not implemented here)

| # | Where | Change | Needed for |
|---|---|---|---|
| A1 | `core/config.py::_llm_configured` | In production, accept Bedrock (`bedrock_model_id` and `aws_region`) as "an LLM is configured". Today a Bedrock-only production deploy refuses to boot without `GOOGLE_API_KEY`. Also let the migration entrypoint validate only DB settings; the migrate task currently runs with `AEGIS_ENV=staging` to avoid needing app secrets. | Bedrock-only deploys; cleaner migrate task |
| A2 | `api/security.py::FirebaseVerifier.initialise` | Accept `FIREBASE_SERVICE_ACCOUNT_JSON` (inline JSON → `credentials.Certificate(dict)`) and keep the file path as a fallback. Then delete the `sh -c` wrapper in `compute.tf`. | Removes a plaintext credential file |
| A3 | `worker/main.py` (`_shutdown`, `reclaim_own`) | On SIGTERM, after a drain deadline under 110 s (stopTimeout is 120), **requeue this worker's in-flight jobs**; checkpoints make the resume safe. On Fargate the hostname changes per task, so `reclaim_own` never matches and a Spot interruption stalls the incident for `reap_stale`'s 900 s. | Spot workers without 15-min stalls |
| A4 | `container.py` (`connect`, `ensure_graph_schema`, telemetry clients) | Treat an empty `NEO4J_URI`, `REDIS_HOST`, `PROMETHEUS_URL`, `TEMPO_URL` or `LOKI_URL` as **"not configured"**: no connect attempt, no 4× retry (~15 s) on boot, and a reason string saying so. Terraform points them at `*.invalid` today, which reports "unavailable". That is not wrong, but it does not distinguish "not deployed" from "down". | Invariant 6 fidelity, faster boot |
| A5 | `api/routers/war_room.py::_integrations` | Report the **worker's** capability report (persisted in Postgres by the worker at start) instead of the API process's settings. Then drop `NIMBLE_API_KEY`, `BFL_API_KEY` and `RAWTREE_READ_KEY` from `api_secret_keys`. | Least privilege on the internet-facing task |
| A6 | `api/app.py` CORS | Optional `CORS_ALLOWED_ORIGIN_REGEX` for Vercel preview URLs, with the production validator refusing overly broad patterns. | Only if previews must call the API |
| A7 | `api/routers/war_room.py::_require_local` | If demo controls are wanted on AWS, gate them on an explicit `DEMO_CONTROLS_ENABLED` flag that is refused when `aegis_env=production`, rather than on `AEGIS_ENV=local`. | Cloud fault-injection demo only |
| A8 | `integrations/runtime.py::EcsAdapter.rollback_deployment` / `get_service` | Map semantic versions to task-definition revisions, for example with an `aegis.version` tag on each revision. Set `ECS_CLUSTER`, and add a scoped `ecs:UpdateService` grant for the workload cluster only. | ECS runtime writes |
| A9 | new `evidence/archive.py` (or `persistence/`) | Optional S3 archive writer using `AEGIS_ARTIFACTS_BUCKET` (already injected) for FLUX images and large evidence blobs under `evidence/` and `investigations/`. | Using the provisioned bucket |
| A10 | `persistence/db.py::Database.connect` | Add a `POSTGRES_SSLMODE` setting (default `verify-full` on AWS) and ship the RDS CA bundle in the image. asyncpg's default `prefer` encrypts (and `rds.force_ssl` guarantees TLS) but does not verify the server certificate. | Hardening |
| A11 | `frontend/src/lib/api.ts::subscribeIncident` | Uses a native `EventSource` with `?access_token=`, but `api/deps.py::current_principal` reads only the `Authorization` header. The incident-page stream therefore returns 401 on **every** deployment. Switch it to the fetch-based client in `lib/war-room/sse.ts`. | Incident live view (pre-existing bug) |
| A12 | `backend/Dockerfile` + build context | Copy `eval/scenarios` into the image, which needs a repo-root build context, if the evaluation catalogue page should work on AWS. | Evaluation page |
| – | SQS driver | **Not recommended now** (§3.4). | – |

---

## 8. Bring-up (operator)

1. Review `hackathon.tfvars`. It holds non-secret values only, and it is
   ignored by the repo's `*.tfvars` rule, so commit it with `git add -f`. It is
   pre-filled with:
   - `cors_allowed_origins = https://aegis-hackathon-kohl.vercel.app`, the
     repository homepage and Vercel production alias.
   - `firebase_project_id = aegis-ai-detective`, taken from the deployed
     bundle's `authDomain`. Confirm it in the Firebase console.
   - **both desired counts at 0**.

   Add `budget_emails` if you want budget alerts.
2. Run the Terraform:
   ```bash
   cd infra/terraform/environments/hackathon
   terraform init
   AWS_PROFILE=aegis-admin terraform apply -var-file=hackathon.tfvars
   ```
   It creates 68 resources (including the ECS service-linked role, which this fresh account lacks), about 10–15 min of which is the CloudFront
   distribution and VPC origin.
3. Populate the secret. The key list is in
   `terraform output app_secret_required_keys`.
   ```bash
   aws secretsmanager put-secret-value --secret-id aegis-hackathon/app \
     --secret-string file://app-secret.json --profile aegis-admin --region us-west-2
   ```
   `app-secret.json` holds the keys `ALERT_INGEST_TOKEN` (`openssl rand -hex 32`),
   `GOOGLE_API_KEY`, `RAWTREE_WRITE_KEY`, `RAWTREE_READ_KEY`, `NIMBLE_API_KEY`,
   `BFL_API_KEY` and `FIREBASE_SERVICE_ACCOUNT_JSON` (the service-account JSON as
   a string). Never commit the file, and delete it after use.
4. In GitHub, create the Environment `aegis-hackathon` and set these variables:
   - `HACKATHON_AWS_ROLE_ARN`
   - `HACKATHON_AWS_REGION` (`us-west-2`)
   - `HACKATHON_ECR_REPOSITORY`
   - `HACKATHON_ECS_CLUSTER`
   - `HACKATHON_API_BASE_URL`

   All come from `terraform output`, except the region.
5. Run **deploy-hackathon** with `bring_up = true`.
6. Set `api_desired_count = 1`, `worker_desired_count = 1` and
   `image_tag = <deployed sha>` in the tfvars, and `terraform apply`. The counts
   are now Terraform's again; without this step, the next apply would scale the
   services back to 0.
7. In Vercel, set `NEXT_PUBLIC_API_BASE_URL` to the CloudFront URL and
   `NEXT_PUBLIC_AEGIS_ENV=production`, then redeploy. The production bundle
   inspected on 2026-09-26 still points at `http://localhost:8000`, which means
   the variable is unset.
8. **Verify SSE through CloudFront by hand.** Copy a Firebase ID token from the
   browser, then run:
   `curl -N -H "Authorization: Bearer $T" "$API/v1/war-room/stream"`.
   It should print `health` frames every 5 s for more than 2 minutes without
   disconnecting.
9. Give demo users roles by setting the Firebase custom claim
   `aegis_roles = ["approver"]` (or `["admin"]`) with the Admin SDK.

---

## 9. Shutdown procedure

| Level | How | Leaves billing | ≈ $/day |
|---|---|---|---|
| Running | – | everything | 2.0 (Spot) / 2.3 |
| **Parked** | counts → 0 in tfvars, then `terraform apply` | ALB, RDS, secrets, ECR | 1.07 |
| Parked + DB stopped | then `aws rds stop-db-instance --db-instance-identifier aegis-hackathon-postgres` (**auto-restarts after 7 days**) | ALB, RDS storage | 0.68 |
| **Destroyed** | `terraform destroy -var-file=hackathon.tfvars` | nothing* | 0 |

\* `force_destroy` empties the S3 bucket and ECR repository, and the secret has
a 0-day recovery window. Snapshots are skipped unless
`db_skip_final_snapshot = false`. Check afterwards with
`aws resourcegroupstaggingapi get-resources --tag-filters Key=environment,Values=hackathon`.
Destroy removes the OIDC provider this root created.

**Known issues in the shared Terraform (not fixed here, out of scope):**

- `modules/observability` filters its budget with `"user:environment$${var.environment}"`.
  In HCL, `$${` is the escape for a literal `${`, so the filter is the text
  `user:environment${var.environment}` and **matches nothing**. This was checked
  with a scratch configuration. The fix is
  `format("user:environment$%s", var.environment)`, which is what this root uses.
- `modules/datastores` pins `engine_version = "16.4"`, which is not orderable in
  us-west-2.
- `ci-terraform.yml` does not validate `environments/hackathon`; add it to the
  `validate` matrix.
