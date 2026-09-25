# Security posture of the pipeline and infrastructure

This covers the delivery pipeline and the AWS estate. The application's own
safety model — evidence validation, risk tiers, the gate chain, untrusted-text
envelopes — is in `ESD.md`, `AIArchitecture.md` and `CLAUDE.md` section 3.

The organising idea is the same as the product's: **the deterministic layer
decides**. An approval is enforced by STS refusing to mint credentials, not by
an `if:` in a YAML file. A missing secret is enforced by ECS refusing to start
a task, not by a log line.

---

## Credentials

### There are no AWS access keys

Not in the repository, not in a GitHub secret, not on a developer's machine for
deployment purposes. Every AWS call from CI uses credentials minted from a
short-lived GitHub OIDC token, scoped by the token's `sub` claim.

There is nothing to rotate and nothing to leak. Verify it holds:

```bash
grep -rIn -E "AKIA[0-9A-Z]{16}|aws_secret_access_key|-----BEGIN" \
  .github infra/terraform docs || echo CLEAN
```

`ci-security.yml` runs `gitleaks` over the **full history** on every pull
request, because a secret committed three commits ago and "removed" at the tip
is still in the pack and still compromised. It runs with `--redact`, so a
finding names the rule, file and line but never the value.

### Trust is scoped by claim, not by convention

The `sub` condition on each role's trust policy is the control. Without it, the
policy accepts a token from any repository on GitHub. Staging accepts
`ref:refs/heads/main` and its two environment subjects; production accepts
**only** environment subjects, so a push to `main` cannot assume the production
deploy role even if a workflow file were changed to try.

### Two roles with different powers

`aegis-<env>-github-plan` is assumed by pull-request jobs. It holds AWS
`ReadOnlyAccess` **plus an explicit Deny** on `secretsmanager:GetSecretValue`,
`ssm:GetParameter*` and `kms:Decrypt` — because `ReadOnlyAccess` includes
reading secret values, and a job running a contributor's branch has no business
doing that.

`aegis-<env>-github-deploy` can create infrastructure. It is broad, as any
Terraform apply role is, and the guardrails are explicit rather than implied:

* `aws:RequestedRegion` condition on every regional service.
* IAM actions limited to `arn:aws:iam::<acct>:role/aegis-<env>-*`.
* `iam:PassRole` limited to those roles *and* to
  `iam:PassedToService = ecs-tasks.amazonaws.com`.
* `Deny` on `iam:CreateUser`, `iam:CreateAccessKey`,
  `iam:CreateOpenIDConnectProvider`, `iam:DeleteOpenIDConnectProvider`,
  `iam:UpdateAssumeRolePolicy`, `organizations:*` and `account:*` — nothing in
  a deploy needs to change who is allowed to deploy.
* `Deny` on `s3:DeleteBucket`, `s3:PutBucketVersioning` and
  `s3:PutBucketPolicy` against the state bucket. A role that can delete the
  state bucket can erase the record of everything it built, which is the one
  failure with no recovery path.

---

## Secrets

### Terraform creates containers, never values

Every secret is an `aws_secretsmanager_secret` with no
`aws_secretsmanager_secret_version`. An operator writes the value with
`aws secretsmanager put-secret-value`. No secret material passes through
Terraform, so none appears in state, in an uploaded plan artifact, or in a CI
log.

The RDS master password goes one better: `manage_master_user_password = true`
has RDS generate and own it. Terraform never sees it at all.

### An empty container fails closed

ECS cannot start a task whose secret does not resolve. A secret container with
no version therefore **prevents the service from running**.

That is the intended behaviour, not an inconvenience. A control plane running
with an unset `ALERT_INGEST_TOKEN` would accept unauthenticated alerts. Failing
to start is the correct outcome (CLAUDE.md invariant 5).

### Optional integrations are absent, not blank

`optional_secrets` is a list. Only what is named there gets a container and an
injected value. There is no placeholder like `"unset"` or `""` for GitHub,
Slack or LangSmith — because an integration reporting "not configured" and an
integration returning nothing are different states, and collapsing them is the
operational bug this platform exists to catch (CLAUDE.md invariant 6).

### The execution role reads secrets; the application does not

Secrets are granted to the **ECS task execution role**, which resolves them
before the container starts. The API and worker task roles hold no
`secretsmanager:*` permission at all, so a compromised application process
cannot enumerate or re-read its own credentials.

### The Firebase credential file

A known sharp edge. `backend/src/aegis/api/security.py` loads Firebase
credentials from `FIREBASE_SERVICE_ACCOUNT_PATH`, a filesystem path, and
Fargate can inject a secret as an environment variable but not as a file.

`modules/compute` bridges it. When `firebase_secret_arn` is set, the container
command becomes:

```sh
umask 077; printf '%s' "$FIREBASE_SERVICE_ACCOUNT_JSON" > /tmp/firebase-service-account.json; \
unset FIREBASE_SERVICE_ACCOUNT_JSON; exec uvicorn aegis.api.app:app ...
```

* `printf '%s' "$VAR"` writes without echoing.
* `umask 077` makes the file readable only by the task's own user.
* `unset` removes it from the environment the application inherits.
* `exec` replaces the shell, so SIGTERM still reaches uvicorn for a graceful
  drain rather than being swallowed by a wrapper process.

The value lands on the task's writable layer, which is per-task and destroyed
with it. It is still a plaintext credential on a filesystem, which is one more
place than necessary. **The durable fix is for the backend to accept the
service account as inline JSON**, at which point this wrapper is deleted.

Leaving `enable_firebase = false` is also safe: the API starts, logs that
Firebase is unconfigured, and refuses every authenticated request.

---

## IAM: separate roles for separate jobs

One "aegis task role" would mean the worker that runs agent-authored tool calls
holds every permission the API needs, and the reverse.

| Role | Can | Cannot |
|---|---|---|
| execution | pull images, resolve named secrets, write to the log group | anything else; the application never assumes it |
| api task | `sqs:SendMessage` on one queue, read/write three S3 prefixes, `ecs:Describe*` on observed clusters | consume the queue, delete artifacts, change anything |
| worker task | consume one queue, read the DLQ, read/write four S3 prefixes, read logs and ECS state on observed clusters | delete dead letters, change anything (unless explicitly enabled) |
| migrate task | write logs | everything else; a migration talks to Postgres and to no AWS API |
| graph task | write logs | everything else; Neo4j calls no AWS API |

Three details worth keeping:

**S3 is prefix-scoped, not bucket-scoped.** `s3:PutObject` on
`<bucket>/evidence/*`, with `s3:ListBucket` conditioned on `s3:prefix`. The
difference between "the worker can write its outputs" and "the worker can
delete every piece of archived evidence in the account".

**Neither role can delete an object.** Evidence an operator can see is evidence
an operator can still audit. The lifecycle rules expire artifacts; the
application does not.

**The write path to the observed environment is off by default.**
`allow_remediation_actions` defaults to `false`, so even a fully compromised
worker holds no permission to change a service in the environment Aegis
watches. Turning it on is one switch; the application's `AUTONOMY_ENABLED` is
another, and they are owned by different people in different systems on
purpose. This is CLAUDE.md invariant 5 expressed in IAM rather than in Python.

The `ecs-tasks.amazonaws.com` trust policies carry `aws:SourceAccount` and
`aws:SourceArn` conditions. Without them the trust policy is a confused-deputy
invitation.

---

## Encryption

**At rest.** RDS storage and its master credential secret use a
customer-managed KMS key with rotation enabled. EFS is encrypted. ElastiCache
is encrypted at rest and in transit. SQS uses SQS-managed SSE. DynamoDB is
encrypted.

S3 is split by what the bucket holds:

| Bucket | Encryption | Why |
|---|---|---|
| artifacts | **SSE-KMS**, environment CMK, bucket key on | Archived evidence and execution outputs |
| ALB access logs | SSE-S3 (AES256) | AWS log delivery writes it; request metadata only |
| Terraform state (bootstrap) | SSE-S3 (AES256) | Created before any CMK exists |

The artifacts bucket was moved from SSE-S3 to the environment's
customer-managed key deliberately. It is the archive half of the evidence
store — the objects a diagnosis cites and the raw output of every action the
executor ran — and under *no claim without evidence* that data is what makes a
past incident auditable. A CMK buys two things an AWS-owned key does not:
every decrypt lands in CloudTrail against a named principal, and revoking the
key revokes access to the ciphertext without editing a single IAM policy.

The cost objection does not survive contact with the details. The key already
exists (the environment root creates one for RDS, the RDS master credential
and Secrets Manager), so there is no second key to manage and no extra
$1/month. `bucket_key_enabled` collapses per-object `GenerateDataKey` calls
into one per bucket-key lifetime, which is what keeps request charges near
zero on a replay-heavy read pattern. The API and worker task roles carry
`kms:Decrypt` and `kms:GenerateDataKey` scoped by `kms:ViaService = s3.<region>`
— usable only through S3, so it cannot become a general decrypt capability
against secrets or RDS snapshots under the same key.

The other two buckets stay on SSE-S3, and `.trivyignore` records why: the
state bucket is created by `bootstrap` with local state before any KMS key
exists, and the ALB log bucket is written by an AWS service principal whose
key-policy grant cannot be verified without a real account and whose failure
mode is access logs silently not being written.

**In transit.** `rds.force_ssl = 1` makes the database refuse a plaintext
connection at the server. Every bucket and both queues carry a
`Deny ... aws:SecureTransport = false` statement, so plaintext access is
refused rather than discouraged.

### TLS at the edge

The ALB has three possible states, and only one of them is fit to serve real
traffic.

| `certificate_arn` | `allow_insecure_http` | Result |
|---|---|---|
| set | *(ignored)* | HTTPS on 443, `ELBSecurityPolicy-TLS13-1-2-2021-06` (TLS 1.2 minimum), port 80 permanently redirects (301) to it |
| empty | `false` *(default)* | **No listener at all.** An `aws_lb` precondition says so at plan time |
| empty | `true` | Plaintext HTTP on 80. Explicitly not production-ready |

`certificate_arn` is **required** in production by a variable validation, and
production hardcodes `allow_insecure_http = false` at the module call rather
than exposing it as a variable — so no tfvars file, no CI input and no
operator in a hurry can turn TLS off on the way past.

The plaintext path exists because issuing an ACM certificate requires DNS
ownership of the API domain, which this project does not yet have, and a
staging environment that cannot come up at all is not a useful staging
environment. It is off by default because a public ALB on plain HTTP puts
every Firebase ID token and the alert ingest token on the wire in clear text,
readable by anything on the path. Turning it on is a recorded decision about a
disposable environment, never a posture that gets promoted.

When neither is set, `api_base_url` is the empty string rather than a URL that
refuses every connection — an empty value fails visibly, a dead URL does not.

---

## Network

Tasks and databases sit in private subnets with no inbound path from the
internet. Security groups reference each other rather than CIDR ranges: the
database accepts 5432 from the task security group and from nothing else, the
tasks accept 8000 from the ALB security group and from nothing else. A subnet
renumbering cannot silently widen any of it.

Egress from tasks is unrestricted, and that is a real trade stated plainly.
Aegis must reach external LLM providers, LangSmith, GitHub and Slack, none of
which publish stable address ranges. An egress allowlist here would be an IP
list that silently breaks the product whenever a provider changes an address.
The control lives at the application layer, in the configured set of base URLs.

`nat_strategy = "none_public"` puts task ENIs in public subnets. Nothing can
reach them today — the only ingress rules are from the ALB security group — but
a future security group mistake becomes internet exposure instead of nothing.
It is not the default in either environment, and `docs/cost-strategy.md` argues
the trade.

---

## Supply chain

`ci-security.yml` gates every pull request and runs weekly on a cron, because a
dependency that was clean on Monday can have a CVE on Thursday without anyone
touching the repository.

| Check | Fails the build on |
|---|---|
| gitleaks | any finding, over the full history |
| pip-audit | any known vulnerability in the resolved dependency graph |
| npm audit | high or critical |
| trivy config | HIGH/CRITICAL misconfiguration in Dockerfiles, compose or Terraform |
| trivy fs | HIGH/CRITICAL with a fix available |
| trivy image (`docker.yml`) | HIGH/CRITICAL with a fix available, **before the push** |

Images are built, loaded locally, scanned, and only then pushed. A vulnerable
image never reaches ECR, so "it is in ECR" always means "it passed the scan".

ECR repositories use `image_tag_mutability = "IMMUTABLE"`. A commit SHA must
always mean the same bytes: if a tag can be overwritten, "staging and
production run the same tag" stops being a statement about what is actually
running, and a rollback to a known-good tag can land on rewritten content.

Both Dockerfiles already run as a non-root user — `aegis` (uid 10001) for the
backend, `node` for the frontend.

### The backend image ships no package installer

`pip`, `setuptools` and `wheel` are uninstalled from the venv at the end of the
builder stage, and the base image's own `pip` is removed in the runtime stage.

This started as a vulnerability report and ended as a design correction. The
image scan flagged `msgpack 1.1.2` (GHSA-6v7p-g79w-8964, out-of-bounds read)
and `setuptools 70.3.0` (CVE-2025-47273, path traversal). Neither came from
this project's dependency graph: both were entries in `pip/_vendor/vendor.txt`
— pip's own bundled dependencies, a second supply chain that Aegis does not
choose versions for, cannot patch, and had no reason to ship. Pinning
`msgpack>=1.2.1` in `pyproject.toml` fixed the real dependency (verified 1.2.2
in the image) and left the vendored copy untouched, which is how the vendored
tree revealed itself.

Nothing installs a package at run time, so removing the installer costs
nothing and closes both findings at the source rather than suppressing them. A
container that cannot install a package is also one an attacker cannot install
a package into.

Verified on a locally built image: `pip: absent`, `import pip` raises
`ModuleNotFoundError`, the API and worker both still import, and
`trivy image --severity HIGH,CRITICAL --ignore-unfixed` exits 0.

### Suppressions

`PIP_AUDIT_IGNORE` (a repository variable) holds space-separated GHSA/PYSEC
ids. **Record every entry here with a date and an expiry** when you add one:

| Advisory | Added | Why | Expires |
|---|---|---|---|
| _(none)_ | | | |

`npm audit` gates on high and critical only. Moderate findings in a transitive
build-time dependency would block every pull request for weeks with no security
benefit; they are surfaced by a second, non-gating report.

#### `.trivyignore`

Accepted Terraform misconfigurations. Each entry carries its full reasoning in
the file itself; this table is the index. The gate is never weakened to make
them pass — no blanket suppressions, no lowered severity threshold, no
`|| true` on a scan.

| ID | Sev | Resource | Why accepted | Expires |
|---|---|---|---|---|
| AWS-0053 | HIGH | `aws_lb.this`, `internal = false` | This ALB *is* the public API entrypoint. Vercel, operators and alert sources all reach it from the internet. Guarded by SG ingress CIDRs, Firebase token auth, the ingest token and TLS | 2027-09-30 |
| AWS-0104 | CRITICAL | tasks egress `0.0.0.0/0` | LLM providers, LangSmith, GitHub and Slack publish no stable IP ranges. An allowlist fails as a timeout mid-investigation. The control is the application's configured base URLs | 2027-03-31 |
| AWS-0132 | HIGH | state bucket, ALB log bucket | The evidence bucket **was fixed** (CMK). These two cannot be: one predates the key, the other is written by an AWS service principal | 2027-03-31 |
| AWS-0136 | HIGH | `aws_sns_topic.alarms` | Already encrypted with `alias/aws/sns`. A CMK needs key-policy grants for every publishing service, and a missing grant means alarms silently never arrive | 2027-03-31 |

Trivy re-raises an entry after its expiry date and the build fails again, so
each of these has to be argued a second time rather than quietly inherited.
That behaviour was verified rather than assumed: an entry back-dated to 2020
fails the gate.

One caveat is worth knowing. Trivy's plain `.trivyignore` matches on rule ID
only — the path-scoped `.trivyignore.yaml` form is not auto-loaded, and the CI
gate passes no `--ignorefile` — so an accepted ID also covers any *future*
resource that trips the same rule. That is why the list is four entries long,
and why the AWS-0132 entry says in so many words to check a new bucket's
encryption by hand.

---

## What is not covered

* **WAF.** Nothing in front of the ALB beyond security groups. Worth about
  $8/month plus per-request charges once there is a public surface with real
  users.
* **GuardDuty, Security Hub, Config.** Not enabled. They are the right next
  layer and they are not free.
* **SBOM generation and image signing.** Trivy scans; nothing produces an SBOM
  or a cosign signature yet.
* **Secret rotation.** Secrets Manager rotation schedules are not configured.
  KMS key rotation is on; the secret values themselves rotate manually.
* **SHA-pinned actions.** Pinned to major tags today. `docs/cicd.md` explains
  why, and gives the command to resolve real SHAs.
* **Penetration testing.** None of this has been deployed, let alone tested.

---

## Reporting

Security issues in Aegis itself go to the owners in `.github/CODEOWNERS` —
`@aegis/security` owns `backend/src/aegis/api/security.py`, the policy,
execution and verification packages, the workflows, and
`infra/terraform/modules/iam/`. Do not open a public issue.
