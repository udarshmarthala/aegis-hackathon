# Terraform

```
infra/terraform/
  bootstrap/                 state bucket, lock table, OIDC provider, ECR
  modules/
    network/                 VPC, subnets, routing, egress, security groups
    datastores/              RDS PostgreSQL, S3 buckets, optional ElastiCache
    graph/                   Neo4j on Fargate + EFS, or external, or disabled
    queue/                   SQS investigations queue + dead-letter queue
    compute/                 ECS cluster, ALB, task definitions, services, autoscaling
    observability/           SNS, CloudWatch alarms, AWS Budgets
    iam/                     task roles, execution role, GitHub OIDC deploy and plan roles
  environments/
    staging/                 thin composition root
    production/              thin composition root
```

The data module is named `datastores`, not `data`. The repository's
`.gitignore` contains a `data/` rule for local runtime state, and a directory
called `modules/data/` matches it — the whole module would have been silently
untracked. Renaming was cheaper than weakening a secrets-adjacent ignore rule.

**Terraform version:** `>= 1.9.0`. The floor is real, not cosmetic: the
environment roots use variable `validation` blocks that reference *other*
variables, which earlier versions reject at parse time.

**Provider:** `hashicorp/aws ~> 5.70`.

---

## Bootstrap ordering

State infrastructure cannot live in the state it manages. That is the
chicken-and-egg, and it is resolved by running `bootstrap/` with **local
state**.

### 1. Bootstrap (local state)

```bash
cd infra/terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # edit: bucket name must be globally unique
terraform init
terraform apply
terraform output
```

Creates the S3 state bucket (versioned, encrypted, TLS-only, 90-day version
expiry, `prevent_destroy`), the DynamoDB lock table, the GitHub OIDC provider,
and the two ECR repositories with immutable tags and lifecycle policies.

The resulting `terraform.tfstate` is local and gitignored. That is acceptable:
everything it creates carries `prevent_destroy`, and the deploy role is
explicitly denied `s3:DeleteBucket` on the state bucket. If the local state is
ever lost, `terraform import` recovers it. What must not be lost is the bucket
itself, which is why it is protected three different ways.

The OIDC provider and the ECR repositories live here rather than in an
environment root because both are **account-wide singletons**. Two environments
in one account cannot each create the provider, and an image should be built
once and promoted by digest rather than rebuilt per environment.

### 2. Record the outputs

Into GitHub repository variables (see `docs/cicd.md`) and into whatever you
pass to the environment roots:

| Bootstrap output | Goes to |
|---|---|
| `state_bucket`, `lock_table` | `-backend-config`, and the `TF_STATE_BUCKET` / `TF_LOCK_TABLE` variables |
| `state_bucket_arn`, `lock_table_arn` | `tf_state_bucket_arn` / `tf_lock_table_arn` inputs |
| `oidc_provider_arn` | `oidc_provider_arn` input |
| `ecr_repository_urls["aegis/backend"]` | `backend_image` input |
| `ecr_repository_names` | `ECR_REPOSITORY_BACKEND` / `ECR_REPOSITORY_WEB` variables |

### 3. Staging

```bash
cd infra/terraform/environments/staging
terraform init \
  -backend-config="bucket=<state_bucket>" \
  -backend-config="key=staging/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="dynamodb_table=<lock_table>" \
  -backend-config="encrypt=true"

cp staging.tfvars.example staging.tfvars   # edit
terraform apply -var-file=staging.tfvars
```

The first apply uses `image_tag = "bootstrap"`. **No image exists at that tag**,
so the services will not become healthy. That is expected. The first run of
`docker.yml` publishes a real tag and `deploy-staging.yml` rolls onto it.

### 4. Populate the secrets

`terraform output secret_arns_to_populate` lists every container created with
no value. ECS cannot start a task whose secret does not resolve, so **the
services will not run until all of them are set** — which is the intended
fail-closed behaviour. A control plane running with an unset ingest token would
accept unauthenticated alerts.

```bash
aws secretsmanager put-secret-value \
  --secret-id aegis-staging/alert-ingest-token \
  --secret-string "$(openssl rand -hex 32)"

aws secretsmanager put-secret-value \
  --secret-id aegis-staging/internal-signing-key \
  --secret-string "$(openssl rand -hex 32)"

# One container per Gemini key, up to google_api_key_count. Slot 1 has no
# suffix; the rest are -2, -3, -4.
aws secretsmanager put-secret-value \
  --secret-id aegis-staging/google-api-key \
  --secret-string '<key>'

aws secretsmanager put-secret-value \
  --secret-id aegis-staging/google-api-key-2 \
  --secret-string '<key>'

# Two keys, because the Neo4j container wants NEO4J_AUTH="neo4j/<password>"
# and the application wants the bare password.
aws secretsmanager put-secret-value \
  --secret-id aegis-staging/neo4j \
  --secret-string '{"auth":"neo4j/<password>","password":"<password>"}'

# Only when enable_firebase = true.
aws secretsmanager put-secret-value \
  --secret-id aegis-staging/firebase-service-account \
  --secret-file fileb://service-account.json
```

The Postgres password is **not** in that list. RDS generates and owns it
(`manage_master_user_password = true`) and the task definition selects it with
`<secret-arn>:password::`.

### 5. Production

Identical, with `key=production/terraform.tfstate` and
`production.tfvars.example`. `certificate_arn` is **required**: the variable's
validation refuses an empty value, because Firebase ID tokens and the ingest
token cross that boundary on every request.

### 6. Wire up GitHub

Set the repository variables and create the four Environments
(`docs/cicd.md`). After that, deploys come from CI and nobody runs `terraform
apply` from a laptop again.

---

## Conventions

**No committed tfvars.** `.gitignore` excludes `*.tfvars` and permits
`*.tfvars.example`. Real values live on an operator's machine or in CI. CI does
not use a tfvars file at all — the workflows pass `-var="image_tag=<sha>"` and
take everything else from the defaults in `variables.tf`. If you change a value
in a tfvars file that CI must also use, change its default too.

**No secrets in state, where avoidable.** Terraform creates secret *containers*
and never their values. The RDS master password is RDS-managed. What does end
up in state: resource ARNs, endpoints, security group ids. Treat the state
bucket as sensitive anyway.

**Consistent tags.** The provider's `default_tags` applies `project`,
`environment`, `owner` and `managed-by` to everything taggable; each module
adds its own `component`. Activate all five as cost allocation tags in Billing,
or Cost Explorer cannot group by them and the AWS Budget filter matches
nothing.

**Separate rule resources, not inline blocks.** Security group rules are
`aws_vpc_security_group_ingress_rule` / `_egress_rule` resources. Inline
`ingress {}` blocks are replaced wholesale on every change, which briefly drops
traffic during an apply.

**Modules do not reach across each other.** Every module takes inputs and
returns outputs; the environment root is the only place that knows the whole
shape.

---

## Two cycles that are avoided on purpose

Both of these are easy to reintroduce.

**1. The log group ARN.** `modules/compute` creates the CloudWatch log group
(it owns the tasks that write to it). `modules/iam` needs that ARN to grant
write access — and `modules/compute` needs IAM's role ARNs. Referencing one
from the other in both directions is a module cycle.

It is resolved in the environment root's `locals`: a CloudWatch log group ARN
is fully determined by its name, so the root constructs the string and hands it
to IAM, while compute creates the actual group.

```hcl
log_group_name = "/aegis/${local.environment}"
log_group_arn  = "arn:aws:logs:${var.aws_region}:${account_id}:log-group:${local.log_group_name}"
```

**2. Alarms versus the resources they watch.** `modules/observability` takes
`alb_arn_suffix`, `db_instance_id`, service names and queue names as plain
strings and creates alarms only for the ones that are non-empty. It creates no
log group and holds no reference any other module needs, so it can depend on
compute without anything depending back on it.

---

## Adding an environment

1. Copy `environments/staging/` to `environments/<name>/`.
2. Change `local.environment` and `local.name_prefix` in `main.tf`, the
   `environment` tag in `versions.tf`, and the backend key comment.
3. Adjust the `deploy_role_subjects` and `plan_role_subjects` lists to the
   GitHub Environments you will create for it.
4. Add the directory to the `validate` matrix in
   `.github/workflows/ci-terraform.yml`.
5. `terraform init -backend-config="key=<name>/terraform.tfstate" ...`

Production was created exactly this way, which is why the two roots are
structurally identical and differ only in numbers.

---

## Verification

```bash
terraform -chdir=infra/terraform fmt -recursive -check
terraform -chdir=infra/terraform/bootstrap init -backend=false
terraform -chdir=infra/terraform/bootstrap validate
terraform -chdir=infra/terraform/environments/staging init -backend=false
terraform -chdir=infra/terraform/environments/staging validate
terraform -chdir=infra/terraform/environments/production init -backend=false
terraform -chdir=infra/terraform/environments/production validate
```

`-backend=false` matters: validation must not require credentials or a state
bucket, which is what lets `ci-terraform.yml` validate on a pull request from
a fork.

The individual module directories are not separately validatable without their
own `terraform init` — they are consumed through the roots, and validating the
three roots covers all seven modules.

`terraform validate` checks syntax, types, references and provider schema. It
does **not** check whether a plan would succeed against a real account. Nothing
here has been planned or applied against AWS.

---

## What is deliberately not modelled

* **Route 53 and ACM.** The certificate ARN is an input. DNS may live outside
  this account or outside AWS entirely, and a Terraform-managed zone that also
  serves the marketing site is a hostage situation.
* **The Vercel frontend.** Outside AWS, deployed by Vercel's own pipeline.
* **The observed workload.** Aegis watches an environment; it does not own it.
  `observed_cluster_arns` points at clusters someone else created, and the IAM
  policy is read-only unless `allow_remediation_actions` is explicitly set.
* **Backup vaults and cross-region copies.** RDS automated backups and S3
  versioning cover the stated recovery requirement. A vault with a cross-region
  copy is the right next step when there is a stated RPO to meet.
* **WAF.** Worth adding in front of the ALB once there is a public surface with
  real users. It is about $8/month plus per-request charges and was left out of
  a first deployment rather than added unexamined.

---

## Provider lock files

`.terraform.lock.hcl` is committed in each root (`bootstrap/`,
`environments/staging/`, `environments/production/`). It pins the exact
provider version and its checksums, so a CI run and a laptop resolve the same
bits.

A lock generated by `terraform init` records checksums **only for the platform
that generated it**. A lock written on Windows and then used by a Linux runner
can fail to verify. Regenerate for every platform anyone will run on before
committing:

```bash
cd infra/terraform/environments/staging
terraform providers lock \
  -platform=linux_amd64 \
  -platform=darwin_arm64 \
  -platform=windows_amd64
```

Repeat in `bootstrap/` and `environments/production/`. It downloads the
provider once per platform, so it is slow and rarely needed — only when the
provider version changes.

If a lock file is missing entirely, `terraform init` creates one. That is not a
failure, but the result is single-platform; run the command above and commit
the result.
