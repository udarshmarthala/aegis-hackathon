# Alarms, notification routing and the cost budget.
#
# Log groups are NOT here: they are created by the module that owns the tasks
# writing to them, which keeps this module free of any dependency on compute
# and lets it watch resources it does not create.
#
# Every alarm here answers a question an operator would actually ask at 03:00.
# An alarm nobody acts on trains people to ignore the channel it posts to.

locals {
  tags = merge(var.tags, { component = "observability" })

  alb      = var.alb_arn_suffix != ""
  tg       = var.target_group_arn_suffix != ""
  database = var.db_instance_id != ""
  services = var.ecs_cluster_name != ""
  dlq      = var.dlq_name != ""
  queue    = var.queue_name != ""
}

resource "aws_sns_topic" "alarms" {
  name = "${var.name_prefix}-alarms"
  # SNS-managed encryption at rest. A customer key would add KMS grants for
  # every publishing service for no benefit: alarm names are not secrets.
  kms_master_key_id = "alias/aws/sns"
  tags              = merge(local.tags, { Name = "${var.name_prefix}-alarms" })
}

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# --------------------------------------------------------- load balancer ---
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  count = local.alb ? 1 : 0

  alarm_name        = "${var.name_prefix}-alb-5xx"
  alarm_description = "The load balancer itself is returning 5xx: no healthy target, or targets timing out."

  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  count = local.alb ? 1 : 0

  alarm_name        = "${var.name_prefix}-api-5xx"
  alarm_description = "The API is returning 5xx. Distinct from the ELB 5xx alarm: this one means the application answered and failed."

  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  count = local.alb && local.tg ? 1 : 0

  alarm_name        = "${var.name_prefix}-unhealthy-targets"
  alarm_description = "An API task is failing its health check. At desired_count 1 this means the control plane is down."

  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  # A missing datapoint here means the target group has no targets at all,
  # which is the outage this alarm exists to catch. Never "notBreaching".
  treat_missing_data = "breaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  count = local.alb ? 1 : 0

  alarm_name        = "${var.name_prefix}-api-latency"
  alarm_description = "p95 API latency is degraded."

  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.target_response_time_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = var.alb_arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# -------------------------------------------------------------- database ---
# Postgres is the system of record. Everything else in Aegis degrades; this
# does not. These three alarms cover the ways it stops being available.
resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  count = local.database ? 1 : 0

  alarm_name        = "${var.name_prefix}-db-cpu"
  alarm_description = "Sustained database CPU. On a burstable instance this also predicts credit exhaustion."

  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = var.db_instance_id
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  count = local.database ? 1 : 0

  alarm_name        = "${var.name_prefix}-db-storage"
  alarm_description = "Free storage is low. Storage autoscaling has a ceiling; this fires before it is reached."

  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.db_storage_alarm_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    DBInstanceIdentifier = var.db_instance_id
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "db_connections" {
  count = local.database ? 1 : 0

  alarm_name        = "${var.name_prefix}-db-connections"
  alarm_description = "Connection count is high. Usually a leaked pool rather than genuine load."

  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.db_connection_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = var.db_instance_id
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ------------------------------------------------------------- services ---
resource "aws_cloudwatch_metric_alarm" "api_cpu" {
  count = local.services && var.api_service_name != "" ? 1 : 0

  alarm_name        = "${var.name_prefix}-api-cpu"
  alarm_description = "API service CPU is saturated and autoscaling has not caught up."

  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.api_service_name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "api_memory" {
  count = local.services && var.api_service_name != "" ? 1 : 0

  alarm_name        = "${var.name_prefix}-api-memory"
  alarm_description = "API memory is near the task limit; the next allocation is an OOM kill."

  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.api_service_name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "worker_memory" {
  count = local.services && var.worker_service_name != "" ? 1 : 0

  alarm_name        = "${var.name_prefix}-worker-memory"
  alarm_description = "Worker memory is near the task limit. An investigation killed by the OOM reaper leaves a held lease behind."

  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  # Missing data is normal here: with worker_min_count 0 there is no task to
  # report a metric, and an idle environment is not an alarm.
  treat_missing_data = "notBreaching"

  dimensions = {
    ClusterName = var.ecs_cluster_name
    ServiceName = var.worker_service_name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# ---------------------------------------------------------------- queue ---
resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  count = local.dlq ? 1 : 0

  alarm_name        = "${var.name_prefix}-dead-letters"
  alarm_description = "An investigation failed every delivery attempt. One message here is a defect, not noise."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 300
  # Threshold zero, one datapoint: a single dead letter matters. This is not a
  # capacity alarm, it is a correctness alarm.
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = var.dlq_name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "queue_stalled" {
  count = local.queue ? 1 : 0

  alarm_name        = "${var.name_prefix}-queue-stalled"
  alarm_description = "Work has been waiting far longer than an investigation takes. Either no worker is running or every worker is wedged."

  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.queue_age_alarm_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = var.queue_name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  tags          = local.tags
}

# --------------------------------------------------------------- budget ---
# A budget does not cap anything - AWS keeps serving - but it turns a surprise
# invoice into an email on the day the trend changes. The 100% threshold is
# FORECASTED so it arrives while the month can still be influenced.
resource "aws_budgets_budget" "monthly" {
  count = var.budget_limit_usd > 0 ? 1 : 0

  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:environment$${var.environment}"]
  }

  dynamic "notification" {
    for_each = var.budget_thresholds_percent

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value >= 100 ? "FORECASTED" : "ACTUAL"
      subscriber_email_addresses = var.alarm_emails
      subscriber_sns_topic_arns  = [aws_sns_topic.alarms.arn]
    }
  }
}

# AWS Budgets publishes to SNS as a service principal, which the topic must
# explicitly allow.
resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudWatchAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.alarms.arn
      },
      {
        Sid       = "AllowBudgets"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.alarms.arn
      },
    ]
  })
}
