# Hackathon composition root.
#
# Shape (docs/AWS_HACKATHON_ARCHITECTURE.md has the diagram):
#
#   Vercel (browser) --HTTPS--> CloudFront (*.cloudfront.net, free tier)
#                                   | VPC origin, private AWS network
#                                   v
#                              internal ALB --> API task (Fargate, public IP, 1)
#                                                   |
#                                  worker task (Fargate Spot, public IP, 1)
#                                                   |
#                                  RDS PostgreSQL 16 (private subnets)
#
# Reuses modules/network as-is (nat_strategy = "none_public"). The other shared
# modules are not used, each for a concrete reason recorded in the architecture
# doc: compute hardcodes an internet-facing ALB and SQS scaling, iam requires an
# SQS queue and has no Bedrock grant, datastores requires a customer-managed KMS
# key and exports RDS logs into a log group with no retention.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  environment = "hackathon"
  name_prefix = "aegis-hackathon"
  account_id  = data.aws_caller_identity.current.account_id
  partition   = data.aws_partition.current.partition

  log_group_name = "/aegis/${local.environment}"

  firebase_credential_path = "/tmp/firebase-service-account.json"

  # Placeholder host for every soft dependency this environment does not run.
  # RFC 6761 reserves .invalid: it never resolves, so each client fails fast
  # with a name error and Aegis records "source unavailable" - a distinct state
  # from "found nothing" (CLAUDE.md invariant 6) - instead of hanging on a
  # connect timeout against an address that happens to exist.
  absent = "disabled.invalid"

  tags = {
    project      = "aegis"
    environment  = local.environment
    owner        = var.owner
    "managed-by" = "terraform"
  }
}

# --------------------------------------------------------------- network ---
# Two AZs (the ALB minimum), public and private /24s, S3 + DynamoDB gateway
# endpoints, and NO NAT Gateway. Tasks sit in the public subnets with a public
# IPv4 each and accept inbound traffic only from the load balancer's security
# group. RDS stays in the private subnets.
#
# allowed_ingress_cidrs is empty on purpose: the load balancer is internal and
# its only ingress rule is the CloudFront origin-facing prefix list in edge.tf.
module "network" {
  source = "../../modules/network"

  name_prefix                = local.name_prefix
  vpc_cidr                   = var.vpc_cidr
  az_count                   = 2
  nat_strategy               = "none_public"
  enable_interface_endpoints = false
  allowed_ingress_cidrs      = []
  tags                       = local.tags
}

# ------------------------------------------------------------------- ecr ---
# Environment-local rather than the bootstrap's shared "aegis/backend", so this
# root can be destroyed without touching anything a future staging account
# would share.
resource "aws_ecr_repository" "backend" {
  name = "${local.name_prefix}/backend"

  # A commit SHA must always mean the same bytes.
  image_tag_mutability = "IMMUTABLE"

  # Basic scanning is free and runs on push.
  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  force_delete = var.force_destroy_buckets

  tags = merge(local.tags, { component = "compute" })
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Untagged layers are build residue."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Ten tagged images is more rollback depth than a hackathon needs."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}

# --------------------------------------------------------------- secrets ---
# ONE secret holding a JSON object, selected per key by ECS
# ("<arn>:<KEY>::"). Secrets Manager bills $0.40 per secret per month, so ten
# single-value secrets would be $4 for what one JSON document holds for $0.40.
# The fail-closed property survives: ECS refuses to start a task whose selected
# key is missing, exactly as it refuses an empty secret.
#
# Terraform creates the container, never the value. Populate it out of band:
#
#   aws secretsmanager put-secret-value --secret-id aegis-hackathon/app \
#     --secret-string file://app-secret.json      # never committed
#
# Encrypted with the AWS-managed aws/secretsmanager key: no key to pay for, and
# no kms:Decrypt grant needed on the execution role.
resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name_prefix}/app"
  description             = "Aegis hackathon application secrets, one JSON object. Value set out of band, never by Terraform."
  recovery_window_in_days = 0

  tags = merge(local.tags, { component = "security" })
}

# ------------------------------------------------------------ postgres -----
resource "aws_db_subnet_group" "this" {
  name       = "${local.name_prefix}-db"
  subnet_ids = module.network.database_subnet_ids
  tags       = merge(local.tags, { component = "data" })
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.name_prefix}-pg16"
  family = "postgres16"

  # A client that does not negotiate TLS is refused by the server.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }

  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "300000"
  }

  tags = merge(local.tags, { component = "data" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${local.name_prefix}-postgres"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = "aegis"
  username = "aegis"

  # RDS generates and owns the master password; it never enters Terraform
  # state. ECS reads it with the "<arn>:password::" selector.
  manage_master_user_password = true

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [module.network.data_security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = false
  port                   = 5432

  backup_retention_period = var.db_backup_retention_days
  backup_window           = "10:00-11:00"
  maintenance_window      = "sun:11:30-sun:12:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  # Performance Insights' 7-day tier is free; Enhanced Monitoring is not.
  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  monitoring_interval                   = 0

  # No enabled_cloudwatch_logs_exports: RDS would create its own log group with
  # no retention, which is the unbounded bill this repo refuses elsewhere.

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = var.db_skip_final_snapshot
  final_snapshot_identifier = var.db_skip_final_snapshot ? null : "${local.name_prefix}-postgres-final"
  apply_immediately         = true

  tags = merge(local.tags, { component = "data", Name = "${local.name_prefix}-postgres" })
}

# -------------------------------------------------------------------- s3 ---
# Artifact archive. SSE-S3 rather than a customer-managed key: at hackathon
# scale the CMK's two benefits (per-principal decrypt audit, revocation by key)
# are not worth $1/month and a kms:ViaService grant that has never been
# exercised. The staging/production design keeps the CMK; this is a recorded
# divergence, not a new default.
#
# NOTE: the backend does not write to S3 today - evidence and incident-map
# images live in Postgres. The bucket and the task-role grants exist so the
# archive writer (listed in the app-change list) needs no infrastructure change.
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name_prefix}-artifacts-${local.account_id}"
  force_destroy = var.force_destroy_buckets
  tags          = merge(local.tags, { component = "data" })
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-artifacts"
    status = "Enabled"

    filter {}

    # A demo archive: 90 days covers the event and the write-up afterwards.
    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

# ---------------------------------------------------------------- budget ---
resource "aws_budgets_budget" "monthly" {
  count = var.budget_limit_usd > 0 ? 1 : 0

  name         = "${local.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Filtered on the environment tag, which only works once "environment" is
  # activated as a cost allocation tag in Billing. Until then the budget sees
  # nothing - see the human-action list in the architecture doc.
  cost_filter {
    name = "TagKeyValue"
    # format(), not "...$${...}": in HCL "$${" is the escape for a literal
    # "${", so that spelling would filter on the text "${local.environment}"
    # and match nothing.
    values = [format("user:environment$%s", local.environment)]
  }

  dynamic "notification" {
    for_each = length(var.budget_emails) > 0 ? [50, 80] : []

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_emails
    }
  }

  dynamic "notification" {
    for_each = length(var.budget_emails) > 0 ? [100] : []

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = var.budget_emails
    }
  }
}
