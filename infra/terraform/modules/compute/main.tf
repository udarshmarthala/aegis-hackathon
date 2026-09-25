# ECS cluster, load balancer, task definitions, services and autoscaling.
#
# One structural decision drives the rest of this file: Terraform owns task
# DEFINITIONS, and the deployment pipeline owns which REVISION each service
# runs. The services therefore ignore changes to task_definition and
# desired_count.
#
# The alternative - Terraform owning the running revision - makes the two
# things you need most during an incident impossible: rolling back to a
# previous revision without a Terraform run, and letting autoscaling change
# the task count without every subsequent plan wanting to undo it.

locals {
  tags = merge(var.tags, { component = "compute" })

  image = "${var.backend_image}:${var.image_tag}"

  # Written by the API, the worker and the migration task alike, separated by
  # stream prefix. One group means one retention setting to get right and one
  # place to search during an incident.
  log_group = "/aegis/${var.environment}"

  # AEGIS_QUEUE_URL is forward-looking: the backend currently drives work from
  # a Postgres job queue and ignores unknown settings, so publishing the URL
  # here is harmless today and is the only wiring the SQS driver will need.
  base_environment = merge({
    AEGIS_ENV       = var.environment
    AWS_REGION      = var.aws_region
    LOG_FORMAT      = "json"
    API_HOST        = "0.0.0.0"
    API_PORT        = "8000"
    AEGIS_QUEUE_URL = var.queue_url
  }, var.environment_variables)

  api_environment = merge(local.base_environment, {
    OTEL_SERVICE_NAME = "aegis-api"
  })

  worker_environment = merge(local.base_environment, {
    OTEL_SERVICE_NAME  = "aegis-worker"
    WORKER_CONCURRENCY = tostring(var.worker_concurrency)
  })

  migrate_environment = merge(local.base_environment, {
    OTEL_SERVICE_NAME   = "aegis-migrate"
    OTEL_TRACES_ENABLED = "false"
  })

  secrets = [for name, arn in var.secret_arns : { name = name, valueFrom = arn }]

  firebase_secrets = var.firebase_secret_arn == "" ? [] : [
    { name = "FIREBASE_SERVICE_ACCOUNT_JSON", valueFrom = var.firebase_secret_arn }
  ]

  # The backend reads Firebase credentials from a file. Fargate cannot project
  # a secret as a file, so the value is injected as an environment variable and
  # written out by this wrapper before the application starts.
  #
  # `printf %s "$VAR"` does not echo the value, umask 077 keeps the file
  # readable only by the task's own user, and `exec` replaces the shell so
  # SIGTERM still reaches the application for a graceful drain rather than
  # being swallowed by a wrapper process.
  firebase_preamble = var.firebase_secret_arn == "" ? "" : join(" ", [
    "umask 077;",
    "printf '%s' \"$FIREBASE_SERVICE_ACCOUNT_JSON\" > ${var.firebase_credential_path};",
    "unset FIREBASE_SERVICE_ACCOUNT_JSON;",
  ])

  api_command = [
    "uvicorn", "aegis.api.app:app",
    "--host", "0.0.0.0", "--port", "8000",
    "--proxy-headers", "--forwarded-allow-ips", "*",
    "--timeout-graceful-shutdown", "30",
  ]

  worker_command = [
    "python", "-m", "aegis.worker.main",
    "--concurrency", tostring(var.worker_concurrency),
  ]

  # Migrations are applied by the application at boot behind a Postgres
  # advisory lock. This one-off task runs the same code path ahead of the
  # rollout so a schema failure stops the deploy instead of crash-looping the
  # first serving task.
  migrate_command = [
    "python", "-c",
    join("\n", [
      "import asyncio",
      "from aegis.core.config import get_settings",
      "from aegis.persistence.db import Database",
      "from aegis.persistence.migrate import run_migrations",
      "async def main():",
      "    db = Database(get_settings())",
      "    await db.connect()",
      "    try:",
      "        applied = await run_migrations(db)",
      "        print('applied:', applied or 'none (schema already current)')",
      "    finally:",
      "        await db.close()",
      "asyncio.run(main())",
    ]),
  ]

  wrapped_api_command = var.firebase_secret_arn == "" ? local.api_command : [
    "sh", "-c", "${local.firebase_preamble} exec ${join(" ", local.api_command)}"
  ]

  wrapped_worker_command = var.firebase_secret_arn == "" ? local.worker_command : [
    "sh", "-c", "${local.firebase_preamble} exec ${join(" ", local.worker_command)}"
  ]
}

# ------------------------------------------------------------- logging ----
resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { Name = local.log_group })
}

# ------------------------------------------------------------- cluster ----
resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.container_insights
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-cluster" })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  # The default applies to anything started without an explicit strategy,
  # including the one-off migration task. Migrations run on on-demand capacity:
  # a Spot interruption mid-migration is not a risk worth the few cents.
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 0
  }
}

# --------------------------------------------------------- load balancer ---
resource "aws_lb" "this" {
  name               = substr("${var.name_prefix}-alb", 0, 32)
  load_balancer_type = "application"
  internal           = false
  subnets            = var.public_subnet_ids
  security_groups    = [var.alb_security_group_id]

  idle_timeout               = var.alb_idle_timeout
  enable_deletion_protection = var.enable_deletion_protection
  drop_invalid_header_fields = true
  # The API sits behind this and trusts X-Forwarded-For for its correlation
  # logging; desync-prone requests are rejected rather than normalised.
  desync_mitigation_mode = "defensive"

  dynamic "access_logs" {
    for_each = var.alb_logs_bucket == "" ? [] : [1]

    content {
      bucket  = var.alb_logs_bucket
      prefix  = var.name_prefix
      enabled = true
    }
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-alb" })

  # No certificate and no explicit opt-in to plaintext means no listener at
  # all. That is the correct fail-closed outcome, but a load balancer that
  # answers nothing is a confusing way to discover it, so the plan says so.
  lifecycle {
    precondition {
      condition     = var.certificate_arn != "" || var.allow_insecure_http
      error_message = "certificate_arn is empty and allow_insecure_http is false, so this ALB would have no listener. Supply an ACM certificate ARN for HTTPS, or set allow_insecure_http = true to accept a plaintext, explicitly non-production endpoint."
    }
  }
}

resource "aws_lb_target_group" "api" {
  name        = substr("${var.name_prefix}-api", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  # /health/ready, not /health/live. A task whose database connection is gone
  # is alive and useless; taking it out of rotation is the point.
  health_check {
    path                = var.health_check_path
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Long enough for an in-flight investigation request to finish, short enough
  # that a deploy does not stall. SSE connections are closed by the graceful
  # shutdown handler in the application, not waited out here.
  deregistration_delay = 30

  tags = merge(local.tags, { Name = "${var.name_prefix}-api-tg" })

  lifecycle {
    create_before_destroy = true
  }
}

# HTTPS when a certificate exists. TLS 1.3-capable policy; anything older than
# TLS 1.2 is refused outright.
resource "aws_lb_listener" "https" {
  count = var.certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  tags = local.tags
}

resource "aws_lb_listener" "http_redirect" {
  count = var.certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = local.tags
}

# No certificate: plain HTTP, and OFF unless it is asked for by name. Two
# conditions must hold before this listener exists, because a public ALB
# serving plaintext puts Firebase ID tokens and the alert ingest token on the
# wire in the clear. It is a throwaway-environment affordance, never a
# production posture. See docs/security.md, "TLS at the edge".
resource "aws_lb_listener" "http_plain" {
  count = var.certificate_arn == "" && var.allow_insecure_http ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  tags = local.tags
}

# ----------------------------------------------------- task definitions ---
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.api_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.image
    essential = true
    command   = local.wrapped_api_command

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = [for k, v in local.api_environment : { name = k, value = tostring(v) }]
    secrets     = concat(local.secrets, local.firebase_secrets)

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    # The ALB health check decides routing; this one decides whether ECS
    # replaces the task. Deliberately more forgiving than the ALB's, so a
    # transient database blip removes the task from rotation without also
    # killing it.
    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://localhost:8000/health/live || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }

    # Matches uvicorn's 30s graceful shutdown, with room for it to finish.
    stopTimeout = 45

    readonlyRootFilesystem = false
  }])

  tags = merge(local.tags, { Name = "${var.name_prefix}-api" })
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.worker_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = local.wrapped_worker_command

    environment = [for k, v in local.worker_environment : { name = k, value = tostring(v) }]
    secrets     = concat(local.secrets, local.firebase_secrets)

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }

    # The worker serves no HTTP, so there is nothing to probe. Its liveness is
    # visible as job throughput and queue depth, which is what the alarms watch.
    #
    # 120s stop timeout: SIGTERM drains an in-flight investigation, and cutting
    # one off mid-way leaves a lease held until it expires.
    stopTimeout = 120
  }])

  tags = merge(local.tags, { Name = "${var.name_prefix}-worker" })
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name_prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.migrate_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image
    essential = true
    command   = local.migrate_command

    environment = [for k, v in local.migrate_environment : { name = k, value = tostring(v) }]
    # No Firebase secret: a migration does not authenticate anyone.
    secrets = local.secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])

  tags = merge(local.tags, { Name = "${var.name_prefix}-migrate" })
}

# ------------------------------------------------------------ services ----
resource "aws_ecs_service" "api" {
  name            = "${var.name_prefix}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count

  # On-demand only. Interrupting a request-serving task to save a few dollars
  # an hour is a bad trade.
  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }

  # A rollout that never reaches a steady state is rolled back by ECS itself,
  # without waiting for a human to notice the CI job hanging.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100
  health_check_grace_period_seconds  = 90
  enable_execute_command             = true
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.task_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-api" })

  lifecycle {
    # The pipeline owns the running revision (rollback without Terraform) and
    # autoscaling owns the count. Terraform asserting either would undo both.
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [
    aws_lb_listener.https,
    aws_lb_listener.http_plain,
  ]
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name_prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count

  dynamic "capacity_provider_strategy" {
    for_each = var.worker_use_spot ? [1] : []

    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 1
      base              = 0
    }
  }

  dynamic "capacity_provider_strategy" {
    for_each = var.worker_use_spot ? [] : [1]

    content {
      capacity_provider = "FARGATE"
      weight            = 1
      base              = 0
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # 0% minimum healthy: a worker has no traffic to keep serving during a
  # deploy, and the queue holds the work in the meantime.
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0
  enable_execute_command             = true
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.task_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-worker" })

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# --------------------------------------------------------- autoscaling ----
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_desired_count
  max_capacity       = var.api_max_count
  tags               = local.tags
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${var.name_prefix}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    target_value = var.api_cpu_target
    # Scale out quickly, scale in slowly: a premature scale-in during a burst
    # costs latency on the requests that are already queued.
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}

resource "aws_appautoscaling_target" "worker" {
  count = var.worker_scaling_mode == "fixed" ? 0 : 1

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
  tags               = local.tags
}

# --- queue-depth scaling ---------------------------------------------------
# Step scaling, not target tracking. Target tracking cannot scale a service
# from zero: with no running tasks the backlog-per-task metric is undefined and
# the policy has nothing to act on. Step scaling driven by an alarm on absolute
# queue depth works from zero, which is the entire point of scaling to zero.
resource "aws_appautoscaling_policy" "worker_scale_out" {
  count = var.worker_scaling_mode == "sqs_backlog" ? 1 : 0

  name               = "${var.name_prefix}-worker-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    cooldown        = 60
    # The alarm fires at depth >= 1, so bounds are relative to that threshold:
    # 1-5 messages adds one task, 5-20 adds two, beyond 20 adds four.
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 4
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = 4
      metric_interval_upper_bound = 19
      scaling_adjustment          = 2
    }

    step_adjustment {
      metric_interval_lower_bound = 19
      scaling_adjustment          = 4
    }
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  count = var.worker_scaling_mode == "sqs_backlog" ? 1 : 0

  name               = "${var.name_prefix}-worker-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_backlog" {
  count = var.worker_scaling_mode == "sqs_backlog" ? 1 : 0

  alarm_name          = "${var.name_prefix}-worker-backlog"
  alarm_description   = "Investigation work is waiting. Adds worker capacity, including from zero."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # SQS publishes this metric every minute even when the queue is empty, so a
  # missing datapoint means the metric pipeline is broken, not that the queue
  # is idle. Treating it as "not breaching" avoids scaling out on a
  # CloudWatch hiccup.
  treat_missing_data = "notBreaching"

  dimensions = {
    QueueName = var.queue_name
  }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_out[0].arn]
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "worker_idle" {
  count = var.worker_scaling_mode == "sqs_backlog" ? 1 : 0

  alarm_name        = "${var.name_prefix}-worker-idle"
  alarm_description = "Queue has been empty for five minutes. Removes worker capacity, down to the configured minimum."
  namespace         = "AWS/SQS"
  metric_name       = "ApproximateNumberOfMessagesVisible"
  statistic         = "Maximum"
  period            = 60
  # Five consecutive empty minutes. Shorter than this and a worker is killed
  # between two phases of the same investigation while the message is invisible
  # rather than absent.
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = var.queue_name
  }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_in[0].arn]
  tags          = local.tags
}

# --- cpu scaling (fallback while the queue driver does not exist) ----------
resource "aws_appautoscaling_policy" "worker_cpu" {
  count = var.worker_scaling_mode == "cpu" ? 1 : 0

  name               = "${var.name_prefix}-worker-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  resource_id        = aws_appautoscaling_target.worker[0].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    target_value       = 60
    scale_out_cooldown = 60
    scale_in_cooldown  = 600
  }
}
