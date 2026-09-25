# AWS cost analysis: hackathon backend

This prices the design in `docs/AWS_HACKATHON_ARCHITECTURE.md`, as implemented
by `infra/terraform/environments/hackathon/`. Region **us-west-2**, 730
hours/month, list prices, USD.

**How the prices were checked.** Unit prices marked **✓** were read from the AWS
Price List API (`aws pricing get-products`) on **2026-09-26**, publication
2026-09-11. **~** marks an estimate: a usage assumption, or a price the API does
not publish, such as Spot. **?** marks a figure that could not be verified.
Nothing has been deployed, so no row has been checked against a real bill.

## Summary

| | $/month | $/day |
|---|---|---|
| **Fixed, worker on Spot (default)** | **≈ 60** | **≈ 2.0** |
| Fixed, worker on-demand (judging window) | ≈ 70 | ≈ 2.3 |
| Parked (both services at 0) | ≈ 32.5 | ≈ 1.07 |
| Parked and RDS stopped | ≈ 20.8 | ≈ 0.68 |
| Variable AWS usage at demo volume | < 1 | – |
| Bedrock tokens, 100 demo investigations (Sonnet 4.6) | ≈ 50–80 ~ | – |

Confidence in the fixed figure: **±10%**. Every unit price except Fargate Spot
was verified. The remaining uncertainty is the ALB LCU and log-ingest usage
assumptions and the Spot discount. The existing staging design,
re-priced for us-west-2, is ~$120–140/month. It pays for a NAT Gateway
($32.85), self-hosted Neo4j with EFS (~$23), a CMK and an internet-facing ALB.

---

## FIXED: always-on

| Service | Purpose | Configuration | Always-on / bursty | Expected usage | Est. $/month | Cheaper alternative | Reason selected |
|---|---|---|---|---|---|---|---|
| Fargate: API | FastAPI, SSE, auth | ARM64, 0.5 vCPU / 1 GB, on-demand, 1 task | always-on | 730 h | **14.42** ✓ (0.5×$0.03238 + 1×$0.00356 = $0.01975/h) | 0.25 vCPU / 1 GB = $8.51; x86 = $18.02 | Spot would drop every SSE stream on interruption; 0.5 vCPU keeps p99 flat during an investigation |
| Fargate: worker | horizon loop, job poller, heartbeat | ARM64, 0.5 vCPU / 1 GB, **FARGATE_SPOT** | always-on | 730 h | **≈ 5** ~ (on-demand $14.42 ✓; Spot "up to 70% off" is not in the Price List) | scale to zero via SQS: saves ≤ $8.65 but needs an always-on heartbeat task (~$10.86); see architecture §3.4 | Checkpointed jobs make Spot safe; flip to on-demand (+~$9.4) for the judging window |
| Public IPv4 | egress for the 2 tasks, no NAT | 2 × in-use address | always-on | 1,460 address-h | **7.30** ✓ ($0.005/h each) | none that keeps egress: NAT $32.85 ✓ + $0.045/GB ✓ | Egress to Gemini, RawTree, Nimble, BFL and Firebase is required; public IP + tight SG is the cheapest correct option |
| ALB (internal) | HTTP routing, `/health/ready` checks, `/metrics` block | 1 ALB, 2 AZ, internal (no public IPv4) | always-on | 730 h + ~0.2 LCU avg | **≈ 17.60** (16.43 ✓ + LCU ~1.17 ~ at $0.008/LCU-h ✓) | API GW HTTP API ~$1–3, but its 30 s limit and buffering break SSE; NLB costs the same | Only a load balancer can be a CloudFront VPC origin for Fargate; internal saves the $7.30 of 2 ALB IPv4 addresses |
| RDS PostgreSQL 16 | system of record + job queue | `db.t4g.micro`, single-AZ, 20 GB gp3, 3-day backups, PI 7-day | always-on | 730 h | **13.98** ✓ (11.68 + 2.30 at $0.115/GB-mo) | Aurora Serverless v2 ≥ ~$44 at 0.5 ACU; Postgres on the API task: no PITR | Managed backups and PITR for the one hard dependency; Graviton is ~10% cheaper than t3 |
| Secrets Manager | app secrets + RDS master | 2 secrets (one JSON with ECS key selectors) | always-on | 2 secrets, ~100 API calls | **0.80** ✓ ($0.40/secret; $0.05/10k calls) | SSM Parameter Store SecureString (free standard tier), but the RDS-managed secret stays | One JSON secret instead of ~8 separate ones saves ~$2.80; ECS resolves it natively |
| CloudWatch Logs | api/, worker/, migrate/ streams | 7-day retention, Container Insights off | always-on | ~2 GB ingested ~ | **≈ 1.00** ($0.50/GB ingest ✓, $0.03/GB-mo storage ✓) | infrequent-access log class | Short retention; no RDS log export (it would be unbounded) |
| ECR | backend image | immutable SHA tags, keep 10, untagged 1 day | always-on | ~0.2 GB compressed per image, shared layers ~0.5–1 GB ~ | **≈ 0.10** ($0.10/GB-mo ✓) | – | – |
| S3 artifacts | archive (no writer yet) | SSE-S3, versioned, 90-day expiry | always-on | < 1 GB | **≈ 0.02** | – | SSE-S3 instead of a CMK saves $1/month |
| CloudFront | HTTPS front door, `*.cloudfront.net` cert | PriceClass_100, CachingDisabled, VPC origin | always-on | ≪ 1 TB, ≪ 10 M requests | **0.00** ✓ (always-free 1 TB + 10 M req/month; VPC-origin fetches free) | – | Free HTTPS without owning a domain |
| VPC | subnets, IGW, S3 + DynamoDB gateway endpoints | 2 AZ, no NAT, no interface endpoints | always-on | – | **0.00** | – | Gateway endpoints are free and keep ECR layer pulls off the internet |
| AWS Budgets | cost guardrail | 1 monthly budget, tag-filtered | always-on | – | **0.00** ? (first budgets free) | – | – |
| **Total** | | | | | **≈ 60.2** (Spot) / **≈ 69.6** (on-demand worker) | | |

## VARIABLE: usage-driven AWS

| Service | Purpose | Configuration | Expected usage | Est. $/month | Note |
|---|---|---|---|---|---|
| Fargate: migrate task | schema migration per deploy | 0.25 vCPU / 0.5 GB ARM, ~2 min | ~20 deploys | < 0.01 ✓ | $0.0099/h |
| GitHub Actions arm64 runner | image build | `ubuntu-24.04-arm` | ~20 builds | 0.00 | free for public repositories ? |
| Data transfer to internet | task egress to SaaS APIs | – | < 5 GB ~ | 0.00 | first 100 GB/month free (AWS aggregate) ? |
| Cross-AZ | task ↔ RDS in another AZ | – | < 5 GB ~ | < 0.10 | $0.01/GB each way ? |
| RDS CPU credits | `db.t4g` runs in *unlimited* mode | – | only under sustained load (eval suites) | 0 typical | $0.075 per surplus vCPU-hour ? |
| NAT data processing | – | none | – | 0.00 | the design has no NAT |
| ALB LCU above baseline | long SSE connections, bursts | – | – | included above | active connections are a minor LCU dimension at demo scale |

## EXTERNAL: AI and API spend (not billed by AWS, or billed outside the fixed estimate)

| Service | Purpose | Price basis | Expected usage | Est. $/month | Note |
|---|---|---|---|---|---|
| **Amazon Bedrock: Claude Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`, the working fallback) | horizon brain | Anthropic list price $3 / $15 per 1M input/output; cache write 1.25×, cache read 0.1× (**Bedrock "us." cross-region profile rate not published in the Price List API ?**, may differ) | ~25 steps × ~12k input (~8k cached) + ~800 output per investigation ~ | **≈ 0.5–0.8 per investigation → ~$50–80 for 100** | Billed by AWS on the same invoice, and **the tag-filtered budget does not see it** (no resource tags on inference). Hard cap per run: `AGENT_MAX_TOKENS=400000`, `AGENT_MAX_LLM_CALLS=40`, `HORIZON_MAX_STEPS=80` |
| Amazon Bedrock: Claude Sonnet 5 (`us.anthropic.claude-sonnet-5`, primary; currently 403) | horizon brain once enabled | Anthropic list price $2 / $10 per 1M ? on Bedrock | same | ≈ 0.35–0.55 per investigation | Cheaper than 4.6 per token once access is granted |
| Google Gemini (AI Studio) | compactor, embeddings, brain tier 2 | free-tier keys (per-key daily quota) or paid per token ? | compaction per step | 0 on free keys | Outside AWS |
| RawTree | episodic memory, heartbeat store, MCP | vendor plan ? | – | ? | Outside AWS; hackathon sponsor |
| Nimble | web evidence (known issues) | vendor plan ? | 1–2 calls per investigation | ? | Falls back to a committed fixture |
| Black Forest Labs FLUX | incident map image | ~$0.04/image for flux-pro-1.1 ? | ≤ 1 per investigation | ~$4 for 100 ? | Skipped when the key is absent |
| Firebase Auth | sign-in | Spark/Blaze free tier for Google sign-in ? | – | 0 | – |

## OPTIONAL: off by default

| Service | Purpose | Configuration | Est. $/month | Why off |
|---|---|---|---|---|
| Worker on-demand | no Spot interruptions | `worker_use_spot = false` | +9.4 | Worth it for the judging window only |
| ElastiCache Redis | live SSE push, multi-API fan-out | `cache.t4g.micro` | +11.68 ✓ | Postgres polling (5 s) is enough at one API task |
| Neo4j AuraDB Free | topology evidence | external `neo4j+s://` | 0 | Needs an account; node cap (see `docs/cost-strategy.md`) |
| Neo4j on Fargate + EFS | self-hosted topology | 0.5 vCPU / 2 GB | ≈ +23 (staging doc) | Non-authoritative data; not worth it for a demo |
| NAT Gateway | tasks without public IPs | 1 × single-AZ | +32.85 ✓ + $0.045/GB ✓ − 7.30 | 4.5× the cost of two IPv4 addresses |
| Custom domain + ACM (us-east-1) on CloudFront | pin TLS ≥ 1.2, nicer URL | Route 53 zone $0.50 + domain ~$12/year | ~1.50 | Needs a domain |
| AWS WAF on CloudFront | rate limiting, managed rules | 1 web ACL + 2 rules | ≈ +7 + $0.60/M req ? | Low-traffic demo |
| Multi-AZ RDS | AZ failover | `db.t4g.micro` Multi-AZ | +11.68 ✓ | Demo tolerates an AZ event |
| Demo workload on Fargate | ECS runtime-adapter demo | 3 × 0.25 vCPU / 0.5 GB ARM + 3 IPv4 | ≈ +32.6 | Needs app changes A7/A8 |
| EC2 compose host (Plan B) | full INC-043 demo in the cloud | `t4g.medium` + 30 GB gp3 + IPv4 | ≈ 31 total (**instead of** the ~$60 design) | Single host, self-managed Postgres, Docker socket exposure |

## Sources

- AWS Price List API, `aws pricing get-products --region us-east-1`, run 2026-09-26:
  - **AmazonECS**, us-west-2: Fargate ARM vCPU $0.03238/h and GB $0.00356/h; x86
    $0.04048 and $0.004445.
  - **AWSELB**: ALB $0.0225/h and $0.008/LCU-h; NLB LCU $0.006.
  - **AmazonVPC**: public IPv4 in-use and idle $0.005/h.
  - **AmazonEC2**: NAT Gateway $0.045/h and $0.045/GB.
  - **AmazonRDS**: `db.t4g.micro` PostgreSQL single-AZ $0.016/h, Multi-AZ $0.032/h;
    gp3 $0.115/GB-mo.
  - **AWSSecretsManager**: $0.40/secret and $0.05/10k API calls.
  - **AmazonCloudWatch**: $0.50/GB ingest, $0.03/GB-mo storage, $0.10/alarm.
  - **AmazonECR**: $0.10/GB-mo.
- CloudFront pay-as-you-go page (fetched 2026-09-26): always-free 1 TB and 10 M
  requests per month, and "Free for origin fetches from any AWS origin",
  including VPC origins. https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/
- CloudFront VPC origins prerequisites (internet gateway, security group options):
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html
- CloudFront origin response timeout semantics (between packets; default 30 s):
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html
- Forwarding `Authorization` (use `AllViewer` when caching is off):
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/add-origin-custom-headers.html
- Fargate Spot on Graviton (supported since 2024; "up to 70%" discount):
  https://www.amazonaws.cn/en/new/2024/amazon-ecs-supports-amazon-graviton-based-spot-compute-with-amazon-fargate/
- Claude list prices (Sonnet 4.6 $3/$15, Sonnet 5 $2/$10 per MTok) come from
  Anthropic's first-party model table. The Bedrock pricing page did not list
  either model when fetched, so the Bedrock rates are **?**.
- Bedrock inference-profile routing (us-east-1, us-east-2, us-west-2):
  `aws bedrock list-inference-profiles`, read-only, 2026-09-26.
