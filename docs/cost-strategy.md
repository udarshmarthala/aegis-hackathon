# Cost strategy

Every number below is a **list price in `us-east-1`, rounded, and stated so it
can be argued with**. None of it has been validated against a real bill,
because nothing has been deployed. Check the current figures with the AWS
Pricing Calculator before committing to a budget.

The goal is not the cheapest possible deployment. It is a deployment whose cost
is *explained* — where every recurring charge traces back to a decision someone
made on purpose.

---

## Estimated monthly cost

### Staging — roughly **$120-140/month**

| Item | Assumption | $/month |
|---|---|---|
| Fargate API | 1 task, 0.5 vCPU / 1 GB, on-demand, 730h | 18 |
| Fargate worker | Spot, ~60h/month of 1 vCPU / 2 GB | 1-3 |
| Fargate Neo4j | 1 task, 0.5 vCPU / 2 GB, on-demand, 730h | 21 |
| EFS (graph store) | ~5 GB bursting | 2 |
| RDS `db.t4g.micro` | single-AZ, 20 GB gp3 | 14 |
| NAT Gateway | 1 gateway + ~20 GB processed | 34 |
| ALB | 730h + low LCU | 18-22 |
| CloudWatch Logs | ~5 GB ingested, 30-day retention | 3 |
| S3 | a few GB across all prefixes | 1 |
| Secrets Manager | ~7 secrets at $0.40 | 3 |
| KMS | 1 customer-managed key + requests | 1.50 |
| ECR | ~10 GB of images | 1 |
| SQS | well inside the 1M-request free tier | 0 |
| Data transfer out | modest | 2 |

### Production — roughly **$260-300/month**

| Item | Assumption | $/month |
|---|---|---|
| Fargate API | 2 tasks, 1 vCPU / 2 GB, on-demand | 72 |
| Fargate worker | Spot, ~1.5 tasks average of 1 vCPU / 2 GB | 16 |
| Fargate Neo4j + EFS | as staging | 23 |
| RDS `db.t4g.small` Multi-AZ | 50 GB gp3, enhanced monitoring | 60 |
| ElastiCache `cache.t4g.micro` | 1 node | 12 |
| NAT Gateway | 1 gateway + data | 34 |
| ALB | higher LCU | 25 |
| CloudWatch Logs | ~20 GB, 90-day retention | 11 |
| S3, Secrets, KMS, ECR, alarms | | 11 |
| Data transfer out | | 10 |

**Not included, and probably larger than all of it:** external LLM API spend.
That is billed by the provider, not by AWS, and no AWS budget will see it. Cap
it with the agent budgets in `.env` (`AGENT_MAX_LLM_CALLS`,
`AGENT_MAX_TOKENS`), which the supervisor enforces outside agent reach.

Budget defaults: $250 staging, $500 production. The production headroom over
the ~$275 estimate is deliberate — the 80% notification should arrive because
something changed, not because the estimate was optimistic.

---

## Decision 1: NAT Gateway — one shared gateway, and gateway endpoints

**Variable:** `nat_strategy` in both environment roots.
**Default:** `"single"`.

A NAT Gateway is about **$33/month before a byte moves**, which on a $130
staging bill is a quarter of the total. It is the obvious thing to attack.

The usual advice is to replace it with VPC interface endpoints. Priced out, at
this scale, that advice is wrong:

| Option | Fixed $/month | Notes |
|---|---|---|
| One NAT Gateway | ~33 | plus $0.045/GB processed |
| One NAT per AZ | ~66 | removes the egress single point of failure |
| 5 interface endpoints x 2 AZs | ~73 | $0.01/AZ/hour each, plus per-GB processing |
| No NAT, tasks in public subnets | ~0 | task ENIs are internet-addressable; only security groups stand between |

Two things settle it.

**First, interface endpoints cannot do the job at all.** Aegis calls external
LLM providers, LangSmith, GitHub and Slack. Those are the product, not an
optional extra, and PrivateLink only covers AWS services. Removing the NAT in
favour of endpoints would leave the worker unable to reach a model. The
endpoints could only ever *reduce* NAT data-processing charges, never replace
the gateway — and at 5 endpoints for $73 against a $33 gateway, they do not
even do that until roughly 800 GB/month of ECR and log traffic.

**Second, `none_public` is a real security downgrade, not a free win.** Tasks
in public subnets with public IPs have internet-addressable ENIs. Today the
only ingress rules are from the ALB security group, so nothing can actually
reach them — but a future security group mistake becomes internet exposure
instead of nothing at all. That is exactly the kind of trade the brief warns
against, so it is offered, documented, and not the default.

**What is taken for free:** S3 and DynamoDB *gateway* endpoints, always
created. They cost nothing and keep ECR layer pulls (served from S3) and all
artifact traffic off the NAT, which is where most of the per-GB charge would
otherwise come from.

Production also defaults to `"single"`. A NAT AZ failure costs *egress*, and
Aegis is built to degrade when external calls fail — it abstains rather than
guessing. If you would rather pay $33/month than reason about that during an
incident, set `nat_strategy = "per_az"`.

---

## Decision 2: RDS PostgreSQL, not Aurora Serverless v2

**Default:** `db.t4g.micro` (staging), `db.t4g.small` Multi-AZ (production).

Serverless is not automatically cheaper, and for this workload it is clearly
not.

Aurora Serverless v2 bills per ACU-hour. Even at the 0.5-ACU floor, running
continuously, that is roughly **$44/month in compute alone** — more than three
times a `db.t4g.micro` — before storage and before per-request I/O charges,
which are genuinely unpredictable for a system that writes evidence rows
throughout every investigation.

Aurora Serverless v2 pays off for spiky workloads that sit idle between bursts.
Aegis is the opposite shape: a control plane with modest, continuous traffic.
Alert ingestion, health polling and the SSE stream never stop. There is no
trough to scale into.

Auto-pause (scale to 0 ACU) does not rescue it either. A paused cluster has a
cold start, and the thing waiting on it is alert ingestion — the one path that
must never be slow.

Graviton (`t4g`) over `t3`: about 10% cheaper for identical vCPU and memory,
and PostgreSQL has first-class arm64 support. There is no reason to pay for
x86 here.

Multi-AZ in production roughly doubles the instance cost and is the only thing
between an AZ failure and a control plane with no system of record. Aegis
degrades gracefully when every other dependency is gone and not at all when
Postgres is, so this is where the money goes.

---

## Decision 3: Neo4j self-hosted on Fargate

**Variable:** `graph_deployment_mode`.
**Default:** `"ecs_fargate"` — about **$23/month** including EFS.

| Option | $/month | Trade |
|---|---|---|
| Fargate 0.5 vCPU / 2 GB + EFS | ~23 | self-managed, single task, no HA |
| AuraDB Free | 0 | 200k node / 400k relationship cap, no SLA, no VPC peering |
| AuraDB Professional | 65+ | managed, HA, backups |

AuraDB Free's node cap is reachable by a real topology graph, and an evidence
platform should not be one `MATCH` away from a silent ceiling. AuraDB
Professional triples the cost of the graph tier to protect data that is, by
this system's own rules, non-authoritative.

So: self-hosted, single task, EFS-backed, private DNS only. The honest
consequence is that a task replacement means a minute or two with no topology,
during which Aegis records an evidence gap and lowers its confidence — which is
behaviour the platform already implements and already surfaces.

`"external"` is one variable away for anyone who wants AuraDB.

---

## Decision 4: Redis is optional, and off in staging

**Variable:** `enable_redis`.
**Default:** `false` in staging, `true` in production. About **$12/month**.

CLAUDE.md invariant 10 says Redis is never authoritative. In Aegis it is two
things: a cache, and the pub/sub bus that fans SSE events across API replicas
(`backend/src/aegis/api/routers/stream.py`). The API already degrades when it
is absent.

With **one** API task there is nothing to fan out to. The publisher and every
subscriber are the same process, and ElastiCache buys nothing. Staging runs one
API task, so staging does not run Redis.

With **two or more** API tasks it stops being optional, and not for performance
reasons: an event published by task A never reaches a browser connected to task
B, so half the operators watching an incident see a stream that silently stops
updating. That is a correctness failure of exactly the kind this platform
exists to catch in other systems. Both environment roots therefore refuse the
combination:

```hcl
validation {
  condition     = var.enable_redis || var.api_desired_count <= 1
  error_message = "api_desired_count > 1 requires enable_redis = true: ..."
}
```

Postgres `LISTEN`/`NOTIFY` could replace the bus and remove ElastiCache
entirely. That is a backend change, not an infrastructure one, and it is worth
$12/month of consideration later.

---

## Decision 5: workers scale to zero, on Spot

**Variables:** `worker_min_count`, `worker_scaling_mode`, `worker_use_spot`.

Two levers, compounding.

**Spot**, roughly 70% off: `$0.04048/vCPU-hour` becomes about
`$0.01217/vCPU-hour`. Usually a risky trade for anything stateful. Here it is
not: an investigation job is a Postgres row taken with
`FOR UPDATE SKIP LOCKED`, so a two-minute Spot interruption notice releases the
lease and another worker picks the job up. The interruption costs latency,
never work. The API never runs on Spot.

**Scale to zero**, driven by `ApproximateNumberOfMessagesVisible`. An idle
staging environment costs nothing in worker compute.

The price is a cold start of roughly **90-120 seconds**: one 60-second alarm
period, then 30-60 seconds for Fargate to pull and start a task. That is fine
in staging and lands on a real incident in production, so production sets
`worker_min_count = 1` — about $15/month on Spot to remove it.

Scaling is **step scaling**, not target tracking, because target tracking
cannot scale from zero: with no tasks running, backlog-per-task is undefined.
An alarm on absolute queue depth works from zero.

`worker_max_count` is a hard cap on what an incident storm can cost. Eight
production workers at 1 vCPU / 2 GB on Spot is about $0.02/hour each — an
all-day storm at the ceiling is roughly $4.

> **This lever is currently inert.** The backend consumes from Postgres, not
> SQS, so the queue stays empty and the backlog alarm never fires. See
> `docs/aws-architecture.md`, "Known gaps". Until the SQS driver lands, use
> `worker_scaling_mode = "cpu"` with `worker_min_count = 1`.

---

## Decision 6: retention is finite, everywhere

Unbounded retention is not a cost you notice; it is a cost that arrives two
years later as a line item nobody can explain.

* CloudWatch log groups have an explicit `retention_in_days`, and the compute
  module's variable validation **refuses zero**, because zero means "keep
  forever".
* Every S3 prefix has a lifecycle rule ending in an expiration, plus
  `noncurrent_version_expiration` at 30 days so versioning is a recovery
  mechanism rather than an archive.
* Incomplete multipart uploads are aborted after 7 days on every bucket. They
  are invisible in the console and billed like any other storage.
* ECR expires untagged layers after 7 days and keeps the most recent 50 tagged
  images.
* Terraform state keeps 90 days of non-current versions.
* Redis has `snapshot_retention_limit = 0`. There is nothing there worth
  backing up.

---

## Levers not pulled, and why

| Lever | Saving | Why not |
|---|---|---|
| ARM64 Fargate | ~20% of compute | Needs arm64 images from CI. `cpu_architecture` is a variable; flipping it alone produces exec-format errors. Do both or neither. |
| Fargate Compute Savings Plan | up to 20% | A 1- or 3-year commitment on an architecture that has never been deployed. |
| Removing the ALB (API Gateway / Lambda) | ~$20 | Lambda cannot hold an SSE connection for the length of an investigation. |
| Container Insights | costs, does not save | Already disabled. Aegis emits its own OTel metrics; Insights would be a billed second copy. |
| Single-AZ production RDS | ~$30 | The system of record is the one thing that does not degrade gracefully. |
| A third AZ | costs ~$33+ | Two is the ALB minimum; a third buys availability this deployment does not need. |
| Multi-region | costs a lot | Explicitly out of scope. |

---

## Watching it

`modules/observability` creates an AWS Budget filtered on the
`environment` cost-allocation tag, with notifications at 50%, 80% and 100% —
the 100% one **forecast-based**, so it arrives while the month can still be
influenced.

Activate `project`, `environment`, `component` and `owner` as cost allocation
tags in Billing → Cost allocation tags. Until you do, the tags exist on every
resource but Cost Explorer cannot group by them and the budget filter matches
nothing.
