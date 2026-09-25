# State infrastructure and account-level singletons.
#
# The chicken-and-egg: remote state needs a bucket, and the bucket needs to be
# created by something. This configuration is that something, and it runs with
# LOCAL state. Everything else in infra/terraform/ then uses the bucket this
# creates.
#
# Run order, from an empty account:
#   1. terraform -chdir=infra/terraform/bootstrap apply
#   2. record the outputs into GitHub repository variables
#   3. terraform -chdir=infra/terraform/environments/staging init -backend-config=...
#
# Also here, because both are account-wide rather than per-environment:
#   * the GitHub OIDC provider (one per account)
#   * the ECR repositories (an image is built once and promoted by digest)

data "aws_caller_identity" "current" {}

# ------------------------------------------------------- state bucket -----
resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  # No force_destroy. Deleting this bucket orphans every resource Terraform
  # has ever created in this account.
  tags = { Name = var.state_bucket_name }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  # Versioning is the recovery path from a corrupted or truncated state file,
  # which is otherwise unrecoverable.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# State files contain resource attributes, and despite every effort to keep
# secrets out of them (RDS-managed passwords, empty secret containers) they
# should be treated as sensitive. TLS-only is the floor.
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    # 90 days of history. Long enough to recover from a bad apply nobody
    # noticed for a month, short enough that the bucket does not accumulate
    # thousands of versions of a large state file forever.
    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

# --------------------------------------------------------- lock table -----
# S3 native locking (use_lockfile) exists in newer Terraform, but a DynamoDB
# table remains the widely supported mechanism and costs effectively nothing
# on PAY_PER_REQUEST: a few writes per apply.
resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Name = var.lock_table_name }

  lifecycle {
    prevent_destroy = true
  }
}

# -------------------------------------------------------- github oidc -----
# Account-level singleton. Creating it here means the environment roots take it
# as an input and two environments in one account cannot fight over it.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider && var.github_repository != "" ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS validates GitHub's certificate chain against its own trust store, so
  # this value is no longer the security control it once was. The API still
  # requires it, and this is GitHub's published thumbprint.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = { Name = "github-actions-oidc" }
}

# --------------------------------------------------------------- ecr ------
resource "aws_ecr_repository" "this" {
  for_each = toset(var.ecr_repositories)

  name = each.value

  # Immutable tags. A commit SHA must always mean the same bytes: if a tag can
  # be overwritten, "staging and production run the same tag" stops being a
  # statement about what is actually running, and a rollback to a known-good
  # tag can land on rewritten content.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = each.value }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged layers: build residue, billed like anything else."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.ecr_untagged_expiry_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the most recent tagged images; expire older ones."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_max_tagged_images
        }
        action = { type = "expire" }
      },
    ]
  })
}
