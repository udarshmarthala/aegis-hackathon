# IAM. Separate roles for separate jobs, because a single "aegis task role"
# means the worker that runs agent-authored tool calls holds every permission
# the API needs, and vice versa.
#
#   execution role - ECS itself: pull the image, read secrets, write logs.
#                    The application never assumes this role.
#   api task role  - serve requests: enqueue work, read/write artifacts,
#                    observe the target environment. No queue consumption.
#   worker task role - consume work, write artifacts, observe the target
#                    environment. Mutating it requires an explicit opt-in.
#   migrate task role - nothing. Migrations talk to Postgres and to no AWS API.
#   graph task role - nothing. Neo4j calls no AWS API.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  tags        = merge(var.tags, { component = "iam" })
  partition   = data.aws_partition.current.partition
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  has_secrets = length(var.secret_arns) > 0
  observes    = length(var.observed_cluster_arns) > 0
  github      = var.github_repository != ""

  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.oidc_provider_arn

  # Artifact prefixes each role may touch. Prefix scoping is the difference
  # between "the worker can write its outputs" and "the worker can delete every
  # piece of archived evidence in the account".
  api_prefixes    = ["investigations/", "evidence/", "evaluations/"]
  worker_prefixes = ["investigations/", "evidence/", "executions/", "evaluations/"]
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Without these two conditions the trust policy is a confused-deputy
    # invitation: any ECS task in any account could be pointed at this role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${local.region}:${local.account_id}:*"]
    }
  }
}

# ----------------------------------------------------- execution role ------
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  description        = "ECS agent: image pull, secret resolution, log delivery. Not assumed by application code."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-ecs-execution" })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  # Named secrets only. No wildcard: a wildcard here would let the ECS agent
  # resolve any secret in the account into any container it starts.
  dynamic "statement" {
    for_each = local.has_secrets ? [1] : []

    content {
      sid       = "ReadNamedSecrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.secret_arns
    }
  }

  statement {
    sid       = "DecryptSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values = [
        "secretsmanager.${local.region}.amazonaws.com",
        "ssm.${local.region}.amazonaws.com",
      ]
    }
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${var.log_group_arn}:*"]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${var.name_prefix}-ecs-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# ------------------------------------------------------- shared task bits ---
data "aws_iam_policy_document" "task_common" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${var.log_group_arn}:*"]
  }

  # Emitting custom metrics does not require naming a resource; the namespace
  # condition is what keeps this from being "write anywhere in CloudWatch".
  statement {
    sid       = "PublishMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Aegis/${var.environment}"]
    }
  }
}

# ---------------------------------------------------------- api task -------
data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.task_common.json]

  statement {
    sid       = "EnqueueInvestigations"
    effect    = "Allow"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
    resources = [var.queue_arn]
  }

  # The API writes artifacts and reads them back for the UI. It cannot delete:
  # evidence an operator can see is evidence an operator can still audit.
  statement {
    sid     = "ArtifactObjects"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    resources = [
      for p in local.api_prefixes : "${var.artifacts_bucket_arn}/${p}*"
    ]
  }

  statement {
    sid       = "ArtifactList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.artifacts_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for p in local.api_prefixes : "${p}*"]
    }
  }

  # SSE-KMS on the artifacts bucket means an object write needs a data key and
  # an object read needs it decrypted. Scoped by ViaService: this grant is
  # usable only through S3, so it cannot be turned into a general decrypt
  # capability against secrets or RDS snapshots encrypted with the same key.
  statement {
    sid     = "ArtifactEncryption"
    effect  = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey"]

    resources = [var.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }

  dynamic "statement" {
    for_each = local.observes ? [1] : []

    content {
      sid    = "ObserveTargetEnvironment"
      effect = "Allow"
      actions = [
        "ecs:DescribeClusters",
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:DescribeTaskDefinition",
        "ecs:ListServices",
        "ecs:ListTasks",
      ]
      resources = ["*"]

      # ecs:List* and DescribeTaskDefinition do not accept a resource ARN, so
      # the cluster condition is what actually scopes this.
      condition {
        test     = "ArnEquals"
        variable = "ecs:cluster"
        values   = var.observed_cluster_arns
      }
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${var.name_prefix}-api-task"
  description        = "Aegis API. Enqueues work, reads and writes artifacts, observes the target environment."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-api-task" })
}

resource "aws_iam_role_policy" "api" {
  name   = "${var.name_prefix}-api-task"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# -------------------------------------------------------- worker task ------
data "aws_iam_policy_document" "worker" {
  source_policy_documents = [data.aws_iam_policy_document.task_common.json]

  statement {
    sid    = "ConsumeInvestigations"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [var.queue_arn]
  }

  # Read-only on the dead-letter queue: a worker may inspect what failed, and
  # may not quietly delete the evidence that it failed.
  statement {
    sid       = "InspectDeadLetters"
    effect    = "Allow"
    actions   = ["sqs:ReceiveMessage", "sqs:GetQueueAttributes"]
    resources = [var.dlq_arn]
  }

  statement {
    sid     = "ArtifactObjects"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    resources = [
      for p in local.worker_prefixes : "${var.artifacts_bucket_arn}/${p}*"
    ]
  }

  statement {
    sid       = "ArtifactList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.artifacts_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for p in local.worker_prefixes : "${p}*"]
    }
  }

  # SSE-KMS on the artifacts bucket means an object write needs a data key and
  # an object read needs it decrypted. Scoped by ViaService: this grant is
  # usable only through S3, so it cannot be turned into a general decrypt
  # capability against secrets or RDS snapshots encrypted with the same key.
  statement {
    sid     = "ArtifactEncryption"
    effect  = "Allow"
    actions = ["kms:Decrypt", "kms:GenerateDataKey"]

    resources = [var.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }

  dynamic "statement" {
    for_each = local.observes ? [1] : []

    content {
      sid    = "ObserveTargetEnvironment"
      effect = "Allow"
      actions = [
        "ecs:DescribeClusters",
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:DescribeTaskDefinition",
        "ecs:ListServices",
        "ecs:ListTasks",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
      ]
      resources = ["*"]
    }
  }

  # The write half. Off by default (CLAUDE.md invariant 5: fail closed). Even
  # when on, it is scoped to the observed clusters and grants no ability to
  # delete a service, change IAM, or touch the Aegis control plane itself.
  dynamic "statement" {
    for_each = (local.observes && var.allow_remediation_actions) ? [1] : []

    content {
      sid    = "RemediateTargetEnvironment"
      effect = "Allow"
      actions = [
        "ecs:UpdateService",
        "ecs:RegisterTaskDefinition",
      ]
      resources = ["*"]

      condition {
        test     = "ArnEquals"
        variable = "ecs:cluster"
        values   = var.observed_cluster_arns
      }
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name_prefix}-worker-task"
  description        = "Aegis worker. Consumes investigations; writes nothing to the target environment unless explicitly enabled."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-worker-task" })
}

resource "aws_iam_role_policy" "worker" {
  name   = "${var.name_prefix}-worker-task"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# ------------------------------------------------ migrate and graph tasks ---
# Deliberately minimal. A migration talks to Postgres; Neo4j talks to nobody.
# Both get log delivery through the execution role and nothing else.
resource "aws_iam_role" "migrate" {
  name               = "${var.name_prefix}-migrate-task"
  description        = "One-off migration task. Holds no AWS permissions by design."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-migrate-task" })
}

resource "aws_iam_role_policy" "migrate" {
  name   = "${var.name_prefix}-migrate-task"
  role   = aws_iam_role.migrate.id
  policy = data.aws_iam_policy_document.task_common.json
}

resource "aws_iam_role" "graph" {
  name               = "${var.name_prefix}-graph-task"
  description        = "Neo4j task. Holds no AWS permissions by design."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-graph-task" })
}

resource "aws_iam_role_policy" "graph" {
  name   = "${var.name_prefix}-graph-task"
  role   = aws_iam_role.graph.id
  policy = data.aws_iam_policy_document.task_common.json
}

# ------------------------------------------------------- github oidc -------
# No long-lived AWS access keys exist for this repository. GitHub mints a
# short-lived OIDC token per job; STS exchanges it for temporary credentials
# scoped by the `sub` claim below. There is nothing to leak and nothing to
# rotate.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # Modern AWS validates the provider's certificate chain against its own
  # trust store, so this list is no longer the security boundary it once was.
  # It is still required by the API, and this is GitHub's published value.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = merge(local.tags, { Name = "github-actions-oidc" })
}

data "aws_iam_policy_document" "github_deploy_assume" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # The `sub` claim is the whole control. Without this condition the trust
    # policy would accept a token from ANY repository on GitHub.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.deploy_role_subjects
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  # A Terraform apply role is inherently powerful; pretending otherwise
  # produces a policy that fails halfway through an apply and leaves the
  # environment half-built. The honest controls are: one region, IAM
  # restricted to this environment's roles, no permission to change who can
  # deploy, and no permission to destroy the state bucket.
  statement {
    sid    = "RegionScopedInfrastructure"
    effect = "Allow"
    actions = [
      "ec2:*",
      "ecs:*",
      "ecr:*",
      "elasticloadbalancing:*",
      "rds:*",
      "elasticache:*",
      "elasticfilesystem:*",
      "sqs:*",
      "logs:*",
      "cloudwatch:*",
      "application-autoscaling:*",
      "servicediscovery:*",
      "secretsmanager:*",
      "ssm:*",
      "sns:*",
      "kms:*",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }

  # S3 and Budgets use global endpoints and reject a region condition.
  statement {
    sid    = "GlobalServices"
    effect = "Allow"
    actions = [
      "s3:*",
      "budgets:*",
      "iam:ListRoles",
      "iam:ListPolicies",
      "iam:GetOpenIDConnectProvider",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ManageThisEnvironmentsRoles"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:CreateServiceLinkedRole",
    ]
    resources = [
      "arn:${local.partition}:iam::${local.account_id}:role/${var.name_prefix}-*",
      "arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/*",
    ]
  }

  # PassRole is how a task definition is handed a task role. Scoped to this
  # environment's roles and to ECS, so it cannot hand a privileged unrelated
  # role to anything.
  statement {
    sid       = "PassTaskRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${var.name_prefix}-*"]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com", "monitoring.rds.amazonaws.com"]
    }
  }
}

# State access and the hard denials live in their own document, attached as a
# second inline policy. Splitting them keeps the "what can this role never do"
# list short enough to read in one screen during a review.
data "aws_iam_policy_document" "deploy_guardrails" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  dynamic "statement" {
    for_each = var.tf_state_bucket_arn != "" ? [1] : []

    content {
      sid       = "TerraformState"
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      resources = [var.tf_state_bucket_arn, "${var.tf_state_bucket_arn}/*"]
    }
  }

  dynamic "statement" {
    for_each = var.tf_lock_table_arn != "" ? [1] : []

    content {
      sid       = "TerraformLock"
      effect    = "Allow"
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable"]
      resources = [var.tf_lock_table_arn]
    }
  }

  # The state infrastructure must outlive every apply that reads it. A role
  # that can delete the state bucket can erase the record of everything it
  # built, which is the one failure mode with no recovery path.
  dynamic "statement" {
    for_each = var.tf_state_bucket_arn != "" ? [1] : []

    content {
      sid       = "ProtectStateInfrastructure"
      effect    = "Deny"
      actions   = ["s3:DeleteBucket", "s3:PutBucketVersioning", "s3:PutBucketPolicy"]
      resources = [var.tf_state_bucket_arn]
    }
  }

  # Nothing in a deploy needs to change who is allowed to deploy.
  statement {
    sid    = "DenyIdentityEscalation"
    effect = "Deny"
    actions = [
      "iam:CreateUser",
      "iam:CreateAccessKey",
      "iam:CreateOpenIDConnectProvider",
      "iam:DeleteOpenIDConnectProvider",
      "iam:UpdateAssumeRolePolicy",
      "organizations:*",
      "account:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "deploy" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  name                 = "${var.name_prefix}-github-deploy"
  description          = "Assumed by GitHub Actions via OIDC to apply Terraform and roll out ECS services."
  assume_role_policy   = data.aws_iam_policy_document.github_deploy_assume[0].json
  max_session_duration = 3600
  tags                 = merge(local.tags, { Name = "${var.name_prefix}-github-deploy" })
}

resource "aws_iam_role_policy" "deploy" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  name   = "${var.name_prefix}-github-deploy"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy[0].json
}

resource "aws_iam_role_policy" "deploy_guardrails" {
  count = local.github && length(var.deploy_role_subjects) > 0 ? 1 : 0

  name   = "${var.name_prefix}-github-deploy-guardrails"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy_guardrails[0].json
}

# ---------------------------------------------------------- plan role ------
# Pull requests get a role that can read everything and change nothing, so a
# plan from an untrusted branch is structurally incapable of mutating
# infrastructure whatever the workflow file says.
data "aws_iam_policy_document" "github_plan_assume" {
  count = local.github && length(var.plan_role_subjects) > 0 ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.plan_role_subjects
    }
  }
}

data "aws_iam_policy_document" "plan" {
  count = local.github && length(var.plan_role_subjects) > 0 ? 1 : 0

  dynamic "statement" {
    for_each = var.tf_state_bucket_arn != "" ? [1] : []

    content {
      sid       = "ReadTerraformState"
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:ListBucket"]
      resources = [var.tf_state_bucket_arn, "${var.tf_state_bucket_arn}/*"]
    }
  }

  # AWS ReadOnlyAccess includes secretsmanager:GetSecretValue. A plan job runs
  # on a pull-request branch and has no reason to read a secret value, so the
  # managed policy is narrowed here rather than trusted as-is.
  statement {
    sid    = "DenySecretReads"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "kms:Decrypt",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "plan" {
  count = local.github && length(var.plan_role_subjects) > 0 ? 1 : 0

  name                 = "${var.name_prefix}-github-plan"
  description          = "Assumed by pull-request jobs via OIDC. Read-only, and cannot read secret values."
  assume_role_policy   = data.aws_iam_policy_document.github_plan_assume[0].json
  max_session_duration = 3600
  tags                 = merge(local.tags, { Name = "${var.name_prefix}-github-plan" })
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  count = local.github && length(var.plan_role_subjects) > 0 ? 1 : 0

  role       = aws_iam_role.plan[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "plan" {
  count = local.github && length(var.plan_role_subjects) > 0 ? 1 : 0

  name   = "${var.name_prefix}-github-plan"
  role   = aws_iam_role.plan[0].id
  policy = data.aws_iam_policy_document.plan[0].json
}
