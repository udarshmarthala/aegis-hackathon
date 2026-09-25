# IAM: one role per job, as in modules/iam, plus the two things that module
# does not have - a Bedrock grant scoped to two inference profiles, and a
# deploy role narrow enough for a workflow that never runs Terraform.
#
#   execution  - ECS agent: pull the image, resolve two named secrets, write logs.
#   api        - artifact prefixes it serves; ECS Exec. No Bedrock, no queue.
#   worker     - Bedrock (two profiles), artifact prefixes; ECS Exec.
#   migrate    - nothing. A migration talks to Postgres only.
#   deploy     - GitHub OIDC, one environment subject: push one repository,
#                register revisions of three families, run the migrate task,
#                roll two services.
#
# No role can delete an artifact, touch another environment, or change IAM.

locals {
  bedrock_model_ids = distinct(compact([var.bedrock_model_id, var.bedrock_fallback_model_id]))

  # Inference profile ARNs are regional and account-scoped.
  bedrock_profile_arns = [
    for id in local.bedrock_model_ids :
    "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${id}"
  ]

  # "us.anthropic.claude-sonnet-4-6" -> "anthropic.claude-sonnet-4-6", in every
  # region the profile may route to. Foundation model ARNs have no account.
  bedrock_foundation_arns = flatten([
    for id in local.bedrock_model_ids : [
      for region in var.bedrock_inference_regions :
      "arn:${local.partition}:bedrock:${region}::foundation-model/${join(".", slice(split(".", id), 1, length(split(".", id))))}"
    ]
  ])

  api_prefixes    = ["investigations/", "evidence/", "evaluations/"]
  worker_prefixes = ["investigations/", "evidence/", "executions/", "evaluations/"]

  log_group_arn = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:${local.log_group_name}"

  github = var.github_repository != ""
  oidc_provider_arn = (
    var.create_github_oidc_provider
    ? try(aws_iam_openid_connect_provider.github[0].arn, "")
    : var.github_oidc_provider_arn
  )
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy guard: only ECS acting for this account may assume.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:*"]
    }
  }
}

# ------------------------------------------------------- execution role ---
resource "aws_iam_role" "execution" {
  name               = "${local.name_prefix}-ecs-execution"
  description        = "ECS agent: image pull, secret resolution, log delivery. Never assumed by application code."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { component = "iam" })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution" {
  # Named secrets only. Both use the AWS-managed aws/secretsmanager key, whose
  # key policy already allows decryption through Secrets Manager for principals
  # in this account, so no kms:Decrypt statement is needed.
  statement {
    sid     = "ReadNamedSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.app.arn,
      aws_db_instance.this.master_user_secret[0].secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${local.name_prefix}-ecs-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# --------------------------------------------------------- shared bits -----
# ECS Exec (an operator shell into a running task for seeding and debugging).
# The permission to *start* a session is ecs:ExecuteCommand on the operator's
# own identity; these are only the channels the in-task agent opens.
data "aws_iam_policy_document" "ecs_exec" {
  statement {
    sid    = "EcsExecChannels"
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

# ------------------------------------------------------------- api role ----
data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.ecs_exec.json]

  # Read and write, never delete: evidence an operator can see is evidence an
  # operator can still audit.
  statement {
    sid       = "ArtifactObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    resources = [for p in local.api_prefixes : "${aws_s3_bucket.artifacts.arn}/${p}*"]
  }

  statement {
    sid       = "ArtifactList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for p in local.api_prefixes : "${p}*"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name_prefix}-api-task"
  description        = "Aegis API. Serves requests; reads and writes artifact prefixes. No Bedrock."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { component = "iam" })
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.name_prefix}-api-task"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# ---------------------------------------------------------- worker role ----
data "aws_iam_policy_document" "worker" {
  source_policy_documents = [data.aws_iam_policy_document.ecs_exec.json]

  # The brain. Invocation through the two named inference profiles only.
  statement {
    sid       = "InvokeThroughInferenceProfiles"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_profile_arns
  }

  # A cross-region profile forwards the call to the foundation model in
  # whichever region it picks, and IAM evaluates that too. The condition means
  # the foundation models are reachable ONLY via these profiles - this grant
  # cannot be used to invoke them directly on demand.
  statement {
    sid       = "FoundationModelsViaProfilesOnly"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_foundation_arns

    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values   = local.bedrock_profile_arns
    }
  }

  statement {
    sid       = "ArtifactObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:GetObjectVersion"]
    resources = [for p in local.worker_prefixes : "${aws_s3_bucket.artifacts.arn}/${p}*"]
  }

  statement {
    sid       = "ArtifactList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for p in local.worker_prefixes : "${p}*"]
    }
  }

  # Deliberately absent: any ecs:UpdateService / StopTask. The runtime write
  # path has no target on AWS (see "What works on AWS" in the architecture
  # doc), and IAM is the second, independent reason a proposal cannot reach an
  # environment write here. AUTONOMY_ENABLED=false is the first.
}

resource "aws_iam_role" "worker" {
  name               = "${local.name_prefix}-worker-task"
  description        = "Aegis worker. Invokes the Bedrock brain; writes nothing to any environment."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { component = "iam" })
}

resource "aws_iam_role_policy" "worker" {
  name   = "${local.name_prefix}-worker-task"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# --------------------------------------------------------- migrate role ----
# No policy at all. Logs are written by the execution role through the awslogs
# driver; a migration calls no AWS API.
resource "aws_iam_role" "migrate" {
  name               = "${local.name_prefix}-migrate-task"
  description        = "One-off migration task. Holds no AWS permissions by design."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = merge(local.tags, { component = "iam" })
}

# ---------------------------------------------------------- github oidc ----
resource "aws_iam_openid_connect_provider" "github" {
  count = local.github && var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's chain against its own trust store; the API still
  # requires a value and this is GitHub's published one.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = merge(local.tags, { component = "iam", Name = "github-actions-oidc" })
}

data "aws_iam_policy_document" "github_assume" {
  count = local.github ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Exact match on ONE subject. GitHub issues an environment subject only to a
    # job that has passed that Environment's protection rules, so approval is
    # enforced by STS rather than by an `if:` anyone with write access can edit.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  count = local.github ? 1 : 0

  statement {
    sid       = "EcrLogin"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushOneRepository"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.backend.arn]
  }

  # Describe/Register do not support resource-level permissions. Region-scoped
  # so the role is useless everywhere else.
  statement {
    sid    = "TaskDefinitions"
    effect = "Allow"
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:ListTaskDefinitions",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid     = "RunMigrationTaskOnly"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = [
      "arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task-definition/${local.name_prefix}-migrate:*",
    ]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }

  statement {
    sid    = "ObserveAndRollServices"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:UpdateService",
    ]
    resources = [
      "arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:service/${aws_ecs_cluster.this.name}/${local.name_prefix}-api",
      "arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:service/${aws_ecs_cluster.this.name}/${local.name_prefix}-worker",
    ]
  }

  statement {
    sid       = "WatchTasks"
    effect    = "Allow"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task/${aws_ecs_cluster.this.name}/*"]
  }

  # Registering a revision hands it the execution and task roles; PassRole is
  # limited to exactly those four roles and to ECS.
  statement {
    sid     = "PassTaskRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.api.arn,
      aws_iam_role.worker.arn,
      aws_iam_role.migrate.arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  # Tail the migration's output into the job log.
  statement {
    sid       = "ReadMigrationLogs"
    effect    = "Allow"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = ["${local.log_group_arn}:*"]
  }
}

resource "aws_iam_role" "deploy" {
  count = local.github ? 1 : 0

  name                 = "${local.name_prefix}-github-deploy"
  description          = "GitHub Actions deploy for the hackathon environment: push, migrate, roll. Cannot run Terraform."
  assume_role_policy   = data.aws_iam_policy_document.github_assume[0].json
  max_session_duration = 3600
  tags                 = merge(local.tags, { component = "iam" })

  lifecycle {
    precondition {
      condition     = local.oidc_provider_arn != ""
      error_message = "github_repository is set but no OIDC provider is available: set create_github_oidc_provider = true or pass github_oidc_provider_arn."
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  count = local.github ? 1 : 0

  name   = "${local.name_prefix}-github-deploy"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy[0].json
}
