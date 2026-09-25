# CI/CD

Seven workflows in `.github/workflows/`. They are deliberately separate: one
monolithic pipeline means a `ruff` failure waits behind a Neo4j container that
takes forty seconds just to report healthy, and a change to the deploy logic
risks the lint job.

| Workflow | Trigger | What it gates |
|---|---|---|
| `ci-backend.yml` | PR / push to main, `backend/**` | ruff, mypy, unit tests with a coverage floor, integration tests against live Postgres + Redis + Neo4j |
| `ci-frontend.yml` | PR / push to main, `frontend/**` | eslint, `tsc --noEmit`, `next build` |
| `ci-security.yml` | every PR and push, plus a weekly cron | gitleaks, pip-audit, npm audit, trivy config and filesystem scans |
| `ci-terraform.yml` | PR / push to main, `infra/terraform/**` | `fmt -check`, tflint, `validate` in three roots, and a `plan` posted as a PR comment |
| `docker.yml` | push to main | build, trivy image scan, then push to ECR |
| `deploy-staging.yml` | successful `docker` run on main | terraform apply, migrations, rollout, smoke tests |
| `deploy-production.yml` | manual dispatch only | the same, behind a protected GitHub Environment, plus verification and rollback |

Nothing in this repository holds an AWS access key. Every AWS call is made with
credentials minted from a short-lived GitHub OIDC token.

---

## The one thing to understand about the deploy order

```
terraform apply  ->  registers NEW task definition revisions.
                     The ECS services are NOT touched.
migrate          ->  one-off Fargate task on the NEW image, run to completion.
deploy           ->  update-service onto the new revision, wait for stability.
smoke            ->  liveness, readiness, and the auth boundary.
```

`aws_ecs_service` in `modules/compute` declares
`lifecycle { ignore_changes = [task_definition, desired_count] }`. Terraform
owns *what a revision contains*; the pipeline owns *which revision is running*.

That split is what makes two things possible:

* **Rollback without Terraform.** `aws ecs update-service --task-definition
  <family>:<previous-revision>` takes effect immediately and the next
  `terraform plan` does not want to undo it. If Terraform asserted the running
  revision, a rollback would survive only until the next apply.
* **Autoscaling without plan churn.** The worker service scales between zero
  and its ceiling all day. With Terraform asserting `desired_count`, every plan
  would propose resetting it.

Migrations run *before* the rollout, on the new image. The application also
applies migrations at boot behind a Postgres advisory lock
(`backend/src/aegis/persistence/migrate.py`), so the one-off task racing a
straggler is safe by construction — but running it first means a broken
migration fails a deploy step instead of crash-looping the first serving task.

---

## GitHub OIDC to AWS

### The trust policy

Created by `modules/iam`. This is what it renders to, for the staging deploy
role:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": [
          "repo:<owner>/<repo>:ref:refs/heads/main",
          "repo:<owner>/<repo>:environment:aegis-staging",
          "repo:<owner>/<repo>:environment:aegis-staging-destructive"
        ]
      }
    }
  }]
}
```

The `aud` condition is mandatory. Without the `sub` condition the policy
accepts a token from **any repository on GitHub** — the single most common way
an OIDC setup is quietly wide open.

**Production is narrower.** Its subject list contains only the `environment:`
entries, never `ref:refs/heads/main`:

```json
"token.actions.githubusercontent.com:sub": [
  "repo:<owner>/<repo>:environment:aegis-production",
  "repo:<owner>/<repo>:environment:aegis-production-destructive"
]
```

GitHub issues an `environment:` subject only for a job that has already
satisfied that Environment's protection rules. Approval is therefore enforced
by STS, not only by `if:` conditions in a YAML file that anyone with write
access can edit.

### Two roles, not one

| Role | Assumed by | Can |
|---|---|---|
| `aegis-<env>-github-plan` | pull-request jobs | read everything (`ReadOnlyAccess`) **minus** secret values, which are explicitly denied |
| `aegis-<env>-github-deploy` | main and environment jobs | create and change infrastructure in one region; cannot create users or access keys, cannot change the OIDC provider, cannot delete the state bucket |

A plan runs on a pull-request branch, so it runs configuration a contributor
wrote. It gets a role that cannot change anything and cannot read a secret —
note that AWS `ReadOnlyAccess` *does* include `secretsmanager:GetSecretValue`,
which is why the plan role narrows it with an explicit `Deny`.

The deploy role is broad, because a Terraform apply role genuinely is. The
honest controls are: an `aws:RequestedRegion` condition on everything regional,
IAM actions restricted to `aegis-<env>-*` roles, `iam:PassRole` restricted to
ECS, and explicit `Deny` statements on identity escalation and on deleting the
state bucket. They live in `modules/iam/main.tf` under `deploy_guardrails`.

---

## Repository configuration an operator must set

### Secrets — none

There are no required repository secrets. `GITHUB_TOKEN` is provided by Actions
automatically. If you find yourself adding `AWS_ACCESS_KEY_ID`, something has
gone wrong.

### Variables

Settings → Secrets and variables → Actions → Variables. These are
non-sensitive identifiers, which is why they are variables and not secrets;
none of them authorizes anything on its own.

| Variable | Source | Used by |
|---|---|---|
| `AWS_REGION` | your choice, e.g. `us-east-1` | every AWS-touching workflow |
| `TF_STATE_BUCKET` | bootstrap output `state_bucket` | terraform init |
| `TF_LOCK_TABLE` | bootstrap output `lock_table` | terraform init |
| `AWS_DEPLOY_ROLE_ARN` | staging output `github_deploy_role_arn` | docker, deploy-staging, deploy-production |
| `AWS_PLAN_ROLE_ARN` | staging output `github_plan_role_arn` | ci-terraform plan |
| `ECR_REPOSITORY_BACKEND` | `aegis/backend` | docker |
| `ECR_REPOSITORY_WEB` | `aegis/web` | docker |
| `ECS_CLUSTER_STAGING` | staging output `ecs_cluster_name` | ci-terraform plan |
| `ECS_SERVICE_API_STAGING` | staging output `api_service_name` | ci-terraform plan |
| `ECS_CLUSTER_PRODUCTION` | production output `ecs_cluster_name` | deploy-production rollback mode |
| `ECS_SERVICE_API_PRODUCTION` | production output `api_service_name` | deploy-production rollback mode |
| `ECS_SERVICE_WORKER_PRODUCTION` | production output `worker_service_name` | deploy-production rollback mode |
| `API_BASE_URL_PRODUCTION` | production output `api_base_url` | deploy-production rollback mode |

The four `*_PRODUCTION` variables exist so that **rollback works when Terraform
does not**. State trouble is one of the reasons you roll back, and a rollback
path that first needs a successful `terraform output` is not a rollback path.

Optional:

| Variable | Default if unset | Effect |
|---|---|---|
| `BACKEND_COVERAGE_MIN` | `60` | coverage floor for `pytest --cov-fail-under` |
| `PIP_AUDIT_IGNORE` | empty | space-separated GHSA/PYSEC ids to suppress, each with an expiry recorded in `docs/security.md` |

Frontend build arguments, inlined into the client bundle at build time and
public by design — they identify a Firebase project, they do not authorize:
`NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_AEGIS_ENV`,
`NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`,
`NEXT_PUBLIC_FIREBASE_PROJECT_ID`, `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET`,
`NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID`, `NEXT_PUBLIC_FIREBASE_APP_ID`.

### Environments

Settings → Environments. Four of them:

| Environment | Protection | Purpose |
|---|---|---|
| `aegis-staging` | none, or one reviewer | routine staging deploys |
| `aegis-staging-destructive` | **required reviewers** | reached only when a staging plan contains a `delete` |
| `aegis-production` | **required reviewers**, deployment branch restricted to `main` | every production deploy |
| `aegis-production-destructive` | **required reviewers**, ideally a different set | reached only when a production plan contains a `delete` |

Approving a production deploy and approving the deletion of a production
database are different decisions, so they are different gates.

### Branch protection on `main`

Required status checks: `ruff + mypy`, `pytest (unit)`,
`pytest (integration, live datastores)`, `lint + types + build`, `gitleaks`,
`pip-audit`, `npm audit`, `trivy (dockerfiles, terraform, filesystem)`,
`fmt + tflint`, and the three `validate (...)` matrix jobs.

Enable "Require review from Code Owners" too — but replace the placeholder
teams in `.github/CODEOWNERS` with real ones first. An unresolvable team
silently blocks every pull request.

---

## Rollback

### Production, the supported path

Re-run `deploy-production.yml` with `mode: rollback` and the previous
revisions. The *previous* run's job summary prints them; they are also in the
ECS console under the service's deployment history.

```
mode:                     rollback
image_tag:                <the tag you are rolling away from, for the record>
rollback_api_revision:    aegis-production-api:41
rollback_worker_revision: aegis-production-worker:39
```

The rollback path runs no Terraform and no migration. It points the services at
an existing revision, waits for stability, and re-runs verification.

### Production, by hand

```bash
aws ecs update-service \
  --cluster aegis-production-cluster \
  --service aegis-production-api \
  --task-definition aegis-production-api:41

aws ecs wait services-stable \
  --cluster aegis-production-cluster \
  --services aegis-production-api
```

Terraform will not revert this: the service ignores changes to
`task_definition`.

### What a rollback does not do

**It does not revert database migrations.** There is no down-migration
mechanism and there will not be one — an automated rollback of a schema change
against live data turns an outage into a data-loss incident.

The contract is that migrations are additive and backward compatible with the
previously deployed image. A migration that drops a column the previous image
still reads is a defect caught in review, not a rollback problem. The pull
request template asserts this explicitly, and the migration runner rejects a
file whose checksum changed after it was applied.

---

## Resuming a partial deploy

Every job is idempotent, so GitHub's "Re-run failed jobs" resumes rather than
restarts. `terraform apply` applies a saved plan; the migration runner is a
no-op on a current schema and CI asserts that on every run; `update-service`
onto the revision already running is a no-op; the smoke tests are read-only.

For a deploy that failed after the infrastructure was already applied, dispatch
`deploy-staging.yml` manually with `skip_terraform: true` and the same
`image_tag`.

---

## Action pinning

Actions are pinned to major tags (`actions/checkout@v4`,
`aws-actions/configure-aws-credentials@v4`), with third-party actions pinned to
an exact published version where one exists (`aquasecurity/trivy-action@0.28.0`,
`terraform-linters/setup-tflint@v4` with an explicit `tflint_version`).

Full SHA pinning is stricter and is the intended next step. It is not done here
because a SHA written from memory is worse than a tag: a wrong one fails every
run, and a plausible-but-wrong one is a supply-chain hazard dressed as a
hardening measure. Resolve them from the live repository instead:

```bash
gh api repos/actions/checkout/git/ref/tags/v4 --jq .object.sha
```

`.github/dependabot.yml` has the `github-actions` ecosystem enabled, which is
what makes SHA pinning maintainable once adopted — Dependabot rewrites the SHA
and its trailing version comment together.

---

## Things the workflows deliberately do not do

* **No auto-apply from a pull request.** `ci-terraform.yml` plans and comments.
  It holds the read-only plan role and could not apply if it tried.
* **No image build on the way to production.** `deploy-production.yml` verifies
  the tag already exists in ECR. Building on the way to production means the
  artifact tested in staging is not the artifact shipped.
* **No automatic rollback on an alarm.** Verification fails and prints the
  rollback command. An automatic rollback triggered by a noisy alarm can be
  worse than the deploy that triggered it; a human decides.
* **No secret is printed.** Terraform renders sensitive attributes as
  `(sensitive value)`; the plan comment reaches `github-script` through the
  environment rather than being interpolated into the script body; `gitleaks`
  runs with `--redact`, so a finding names the rule, file and line but never
  the value.

---

## Local equivalents

```bash
cd backend && python -m ruff check src tests && python -m mypy src
cd backend && python -m pytest -m "not integration and not chaos" --cov=src/aegis
cd frontend && npm run lint && npx tsc --noEmit && npm run build
terraform -chdir=infra/terraform fmt -recursive -check
terraform -chdir=infra/terraform/environments/staging validate
```
