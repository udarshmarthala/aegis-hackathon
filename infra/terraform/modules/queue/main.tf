# Investigation work queue and its dead-letter queue.
#
# The DLQ is not decoration. A message that fails three times is evidence of a
# defect, and it must be inspectable rather than silently retried forever or
# silently dropped. The alarm on its depth lives in the observability module.
#
# Encryption uses SQS-managed keys (SSE-SQS): encrypted at rest with no KMS
# request charges. A customer-managed key would add a KMS API call per message
# for no gain against this threat model - the queue is private to the VPC and
# access is IAM-scoped.

locals {
  tags = merge(var.tags, { component = "queue" })
}

resource "aws_sqs_queue" "dlq" {
  name = "${var.name_prefix}-investigations-dlq"

  message_retention_seconds = var.dlq_retention_seconds
  sqs_managed_sse_enabled   = true

  tags = merge(local.tags, { Name = "${var.name_prefix}-investigations-dlq" })
}

resource "aws_sqs_queue" "investigations" {
  name = "${var.name_prefix}-investigations"

  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  receive_wait_time_seconds  = var.receive_wait_time_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = merge(local.tags, { Name = "${var.name_prefix}-investigations" })
}

# Only this queue may redrive into the DLQ. Without it, any queue in the
# account could dump messages into a queue an operator trusts to contain only
# real Aegis failures.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.investigations.arn]
  })
}

# TLS-only, on both queues. An SQS call over plaintext HTTP is refused rather
# than merely discouraged.
resource "aws_sqs_queue_policy" "tls_only" {
  for_each = {
    main = aws_sqs_queue.investigations.id
    dlq  = aws_sqs_queue.dlq.id
  }

  queue_url = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = each.key == "main" ? aws_sqs_queue.investigations.arn : aws_sqs_queue.dlq.arn
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}
