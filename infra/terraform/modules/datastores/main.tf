# PostgreSQL (the system of record), S3 artifact storage, and an optional
# Redis cache.
#
# Two decisions are load bearing and are argued in docs/cost-strategy.md:
#   * RDS PostgreSQL on Graviton, not Aurora Serverless v2. A control plane
#     with modest steady traffic never reaches the break-even point where ACU
#     billing beats a small always-on instance.
#   * Redis is optional and off by default. It is a cache and an SSE bus, and
#     Aegis is built to degrade without it.

data "aws_region" "current" {}

locals {
  tags = merge(var.tags, { component = "data" })
}

# ----------------------------------------------------------- postgres ------
resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db"
  subnet_ids = var.subnet_ids
  tags       = merge(local.tags, { Name = "${var.name_prefix}-db" })
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name_prefix}-pg16"
  family = "postgres16"

  # TLS is not optional. rds.force_ssl rejects any non-TLS connection at the
  # server, so a misconfigured client fails loudly instead of sending the
  # system of record's contents across the VPC in plaintext.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Matches the local compose stack, so a query that is slow in production is
  # slow in the logs developers already know how to read.
  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  # An open transaction from a crashed caller must not hold locks forever.
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "300000"
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name_prefix}-postgres"
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  db_name  = var.db_name
  username = var.db_username

  # RDS generates the master password and owns its rotation. The value never
  # passes through Terraform, so it never lands in state, in a plan artifact,
  # or in a CI log. This is the single most effective control against the
  # "secrets in Terraform state" problem.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = var.multi_az
  port                   = 5432

  backup_retention_period = var.backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot   = true

  # Minor versions are applied in the maintenance window; a major version is a
  # reviewed change, never an automatic one.
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  performance_insights_enabled          = var.performance_insights_enabled
  performance_insights_retention_period = var.performance_insights_enabled ? 7 : null
  monitoring_interval                   = var.monitoring_interval
  monitoring_role_arn                   = var.monitoring_interval > 0 ? aws_iam_role.rds_monitoring[0].arn : null

  # postgresql: statement and error logs. upgrade: the log that explains why a
  # version upgrade failed, which is exactly when you cannot get a shell.
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.name_prefix}-postgres-final-${formatdate("YYYYMMDDhhmmss", timestamp())}"

  apply_immediately = var.environment != "production"

  tags = merge(local.tags, { Name = "${var.name_prefix}-postgres" })

  lifecycle {
    # The snapshot name embeds a timestamp, which would otherwise show a diff
    # on every plan. The password is RDS-managed and must never be reverted.
    ignore_changes = [final_snapshot_identifier]
  }
}

resource "aws_iam_role" "rds_monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  name = "${var.name_prefix}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  role       = aws_iam_role.rds_monitoring[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ------------------------------------------------------------- s3 ----------
# One artifacts bucket with prefix-scoped lifecycle rules rather than four
# buckets. Four buckets means four policies, four sets of block-public-access
# settings, and four chances to get one of them wrong.
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy_buckets
  tags          = merge(local.tags, { Name = "${var.name_prefix}-artifacts" })
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-KMS with the environment's customer-managed key, not SSE-S3.
#
# This bucket is the archive half of the evidence store: the artifacts a
# diagnosis cites and the raw outputs of every action the executor ran. Under
# "no claim without evidence" that data is what makes a past incident
# auditable, so it earns the two things a CMK buys over an AWS-owned key -
# every decrypt appears in CloudTrail against a named principal, and revoking
# the key revokes access to the ciphertext without touching a single IAM
# policy.
#
# The key already exists (the environment root creates one for RDS, the RDS
# master credential and Secrets Manager, with rotation on), so this adds no key
# to manage and no monthly key charge. bucket_key_enabled collapses the
# per-object GenerateDataKey calls into one call per bucket key lifetime,
# which is what keeps request charges near zero on a read-back-heavy replay.
#
# The task roles that read and write here are granted kms:Decrypt and
# kms:GenerateDataKey via s3 in modules/iam; without those grants every
# artifact write fails closed with AccessDenied.
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  # Evidence is meant to be immutable. Versioning means an overwrite is
  # recoverable rather than a silent loss of the thing a diagnosis cited.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  dynamic "rule" {
    for_each = var.artifact_retention

    content {
      id     = replace(rule.key, "/", "")
      status = "Enabled"

      filter {
        prefix = rule.key
      }

      transition {
        days          = rule.value.transition_ia_days
        storage_class = "STANDARD_IA"
      }

      transition {
        days          = rule.value.transition_glacier_days
        storage_class = "GLACIER_IR"
      }

      expiration {
        days = rule.value.expiration_days
      }

      # Old versions are a recovery mechanism, not an archive. 30 days is long
      # enough to notice an accidental overwrite.
      noncurrent_version_expiration {
        noncurrent_days = 30
      }
    }
  }

  # Nothing else should be writing here, but an incomplete multipart upload
  # that is never cleaned up is billed forever.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

# TLS-only. An S3 request over plaintext HTTP is rejected by the bucket rather
# than merely discouraged by convention.
resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

# ----------------------------------------------------- alb access logs -----
resource "aws_s3_bucket" "alb_logs" {
  bucket        = "${var.name_prefix}-alb-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy_buckets
  tags          = merge(local.tags, { Name = "${var.name_prefix}-alb-logs" })
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      # The ALB log delivery service cannot write to a bucket encrypted with a
      # customer-managed key without extra key policy work. SSE-S3 keeps this
      # simple and still encrypts at rest.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "expire"
    status = "Enabled"

    filter {}

    expiration {
      days = var.alb_log_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# The ELB log delivery account differs by region; the data source resolves it
# rather than hardcoding an account id that is wrong in half of AWS.
data "aws_elb_service_account" "current" {}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowELBLogDelivery"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.current.arn }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.alb_logs.arn}/*"
      },
      {
        Sid       = "AllowLogDeliveryService"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.alb_logs.arn}/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      },
      {
        Sid       = "AllowLogDeliveryAclCheck"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.alb_logs.arn
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.alb_logs.arn,
          "${aws_s3_bucket.alb_logs.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.alb_logs]
}

# ------------------------------------------------------------ redis --------
# Off unless enable_redis is true. See the variable's description for the one
# condition that makes it mandatory: more than one API task.
resource "aws_elasticache_subnet_group" "this" {
  count = var.enable_redis ? 1 : 0

  name       = "${var.name_prefix}-redis"
  subnet_ids = var.subnet_ids
  tags       = local.tags
}

resource "aws_elasticache_replication_group" "this" {
  count = var.enable_redis ? 1 : 0

  replication_group_id = "${var.name_prefix}-redis"
  description          = "Aegis ${var.environment} cache and SSE fan-out bus. Never authoritative."

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.redis_node_type
  port           = 6379

  # One node. A replica would double the cost to protect data that is by
  # definition reconstructible from Postgres.
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false

  subnet_group_name  = aws_elasticache_subnet_group.this[0].name
  security_group_ids = [var.security_group_id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn

  parameter_group_name = aws_elasticache_parameter_group.this[0].name

  # Nothing here is a system of record, so there is nothing to snapshot.
  snapshot_retention_limit = 0
  apply_immediately        = var.environment != "production"

  maintenance_window = "sun:05:30-sun:06:30"

  tags = merge(local.tags, { Name = "${var.name_prefix}-redis" })
}

resource "aws_elasticache_parameter_group" "this" {
  count = var.enable_redis ? 1 : 0

  name   = "${var.name_prefix}-redis7"
  family = "redis7"

  # Evicting under memory pressure is correct behaviour for a cache, not data
  # loss. The same policy the local compose stack uses.
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}
