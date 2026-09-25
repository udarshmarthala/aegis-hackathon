# ECS cluster, internal load balancer, task definitions and services.
#
# As in modules/compute, Terraform owns task DEFINITIONS and the deploy
# workflow owns which REVISION runs: the services ignore task_definition, so a
# rollback is one `aws ecs update-service` and the next plan does not undo it.

locals {
  image = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"

  # Everything below is configuration, not secret. Secrets arrive through the
  # `secrets` block of each container, resolved by ECS before start.
  common_environment = merge({
    AEGIS_ENV              = var.aegis_env
    AEGIS_ENVIRONMENT_NAME = "aws-hackathon"
    LOG_LEVEL              = "INFO"
    LOG_FORMAT             = "json"
    API_HOST               = "0.0.0.0"
    API_PORT               = "8000"
    API_PUBLIC_URL         = "https://${aws_cloudfront_distribution.api.domain_name}"
    CORS_ALLOWED_ORIGINS   = var.cors_allowed_origins

    POSTGRES_HOST = aws_db_instance.this.address
    POSTGRES_PORT = tostring(aws_db_instance.this.port)
    POSTGRES_DB   = aws_db_instance.this.db_name
    POSTGRES_USER = aws_db_instance.this.username

    # Not run on AWS. Each points at a name that cannot resolve, so the client
    # fails fast and the capability is reported unavailable with a reason.
    # Empty means "not deployed": the app skips the connection entirely and
    # reports each as unconfigured rather than as a dependency that is down.
    REDIS_HOST     = ""
    NEO4J_URI      = var.neo4j_uri
    PROMETHEUS_URL = ""
    TEMPO_URL      = ""
    LOKI_URL       = ""
    # No collector to export to; exporting into the void costs retries.
    OTEL_TRACES_ENABLED = "false"

    # The brain: Bedrock through the task role (botocore's default chain, no
    # keys, no profile), primary model with a verified fallback.
    AWS_REGION                = var.aws_region
    AWS_PROFILE               = ""
    BEDROCK_MODEL_ID          = var.bedrock_model_id
    BEDROCK_FALLBACK_MODEL_ID = var.bedrock_fallback_model_id
    AEGIS_MODE                = "live"
    HORIZON_DRIVER            = "horizon"

    # Observe and recommend. Three independent locks keep the write path shut
    # on AWS: autonomy off here, no runtime target (ECS_CLUSTER empty, so the
    # ECS adapter reports itself unavailable), and no write permission in the
    # worker's IAM role. There is no Docker daemon on Fargate, so the sandbox
    # is off too rather than failing on every call.
    AUTONOMY_ENABLED         = "false"
    AUTH_DEV_MODE            = "false"
    SANDBOX_ENABLED          = "false"
    WORKLOAD_ADAPTER         = "ecs"
    ECS_CLUSTER              = ""
    WORKLOAD_METRICS_TARGETS = ""
    LANGSMITH_TRACING        = "false"

    FIREBASE_PROJECT_ID = var.firebase_project_id

    # Forward-looking, like AEGIS_QUEUE_URL in modules/compute: nothing reads
    # it yet (settings ignore unknown keys); the S3 archive writer will.
    AEGIS_ARTIFACTS_BUCKET = aws_s3_bucket.artifacts.bucket
  }, var.extra_environment_variables)

  db_password_secret = [{
    name      = "POSTGRES_PASSWORD"
    valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:password::"
  }]

  api_secrets = concat(local.db_password_secret, [
    for k in var.api_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }
    ], var.enable_firebase ? [{
      name      = "FIREBASE_SERVICE_ACCOUNT_JSON"
      valueFrom = "${aws_secretsmanager_secret.app.arn}:FIREBASE_SERVICE_ACCOUNT_JSON::"
  }] : [])

  worker_secrets = concat(local.db_password_secret, [
    for k in var.worker_secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }
  ])

  api_command = join(" ", [
    "uvicorn", "aegis.api.app:app",
    "--host", "0.0.0.0", "--port", "8000",
    "--proxy-headers", "--forwarded-allow-ips", "'*'",
    "--timeout-graceful-shutdown", "30",
  ])

  # The backend reads Firebase credentials from a FILE. Fargate cannot project a
  # secret as a file, so it is injected as a variable and written here: no
  # echo, 0600 via umask, unset before the app starts, and `exec` so SIGTERM
  # reaches uvicorn for a graceful drain. Same bridge as modules/compute.
  api_entrypoint = var.enable_firebase ? [
    "sh", "-c",
    "umask 077; printf '%s' \"$FIREBASE_SERVICE_ACCOUNT_JSON\" > ${local.firebase_credential_path}; unset FIREBASE_SERVICE_ACCOUNT_JSON; exec ${local.api_command}",
  ] : ["sh", "-c", "exec ${local.api_command}"]

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

  log_options = {
    "awslogs-group"  = aws_cloudwatch_log_group.this.name
    "awslogs-region" = var.aws_region
  }
}

# ------------------------------------------------------------- logging ----
resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  tags              = merge(local.tags, { component = "compute" })
}

# ------------------------------------------------------------- cluster ----
resource "aws_ecs_cluster" "this" {
  name = "${local.name_prefix}-cluster"

  # Off: a second, billed copy of metrics the service console already shows.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = merge(local.tags, { component = "compute" })

  depends_on = [aws_iam_service_linked_role.ecs]
}

# The account has never run ECS (AWSServiceRoleForECS did not exist on
# 2026-09-26). CreateCluster creates it implicitly, but asynchronously, and a
# capacity-provider association or service created in the same apply can race
# it and fail with "unable to assume the service linked role". Creating it
# explicitly first removes the race. If the role exists by the time you apply
# (another tool created it), import it:
#   terraform import aws_iam_service_linked_role.ecs \
#     arn:aws:iam::<account>:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS
resource "aws_iam_service_linked_role" "ecs" {
  aws_service_name = "ecs.amazonaws.com"
  description      = "ECS service-linked role, created ahead of the first cluster."
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  # Anything started without a strategy - the one-off migration - runs
  # on-demand: a Spot interruption mid-migration is not worth the cents.
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 0
  }
}

# ------------------------------------------------------- load balancer ----
# INTERNAL. Nothing on the internet can resolve it to a reachable address; the
# only way in is CloudFront's VPC origin (edge.tf). An internal ALB also has no
# public IPv4 addresses, which saves the ~$7.30/month an internet-facing one
# costs in address charges across two AZs.
resource "aws_lb" "api" {
  name               = "${local.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = true
  subnets            = module.network.private_subnet_ids
  security_groups    = [module.network.alb_security_group_id]

  # SSE streams are quiet between events. The app sends a heartbeat at least
  # every 15 s, but a 60 s default would still be the tightest timeout on the
  # path for no benefit.
  idle_timeout               = 300
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "defensive"

  tags = merge(local.tags, { component = "compute" })
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.network.vpc_id

  # /health/ready checks Postgres: a task that lost its database is alive and
  # useless, and taking it out of rotation is the point.
  health_check {
    path                = "/health/ready"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = merge(local.tags, { component = "compute" })

  lifecycle {
    create_before_destroy = true
  }
}

# Plain HTTP between CloudFront's VPC origin and the internal ALB: that hop is
# on AWS's private network, never the internet. TLS terminates at CloudFront.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.id
  }

  tags = merge(local.tags, { component = "compute" })
}

# /metrics is unauthenticated by design (it is a Prometheus target). With no
# Prometheus on AWS nothing scrapes it, and publishing process internals to the
# internet buys nothing, so the edge refuses it.
resource "aws_lb_listener_rule" "deny_metrics" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":{\"code\":\"NOT_FOUND\",\"message\":\"not found\"}}"
      status_code  = "404"
    }
  }

  condition {
    path_pattern {
      values = ["/metrics", "/metrics/*"]
    }
  }

  tags = merge(local.tags, { component = "compute" })
}

# ----------------------------------------------------- task definitions ---
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.image
    essential = true
    command   = local.api_entrypoint

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [for k, v in merge(local.common_environment, {
      OTEL_SERVICE_NAME             = "aegis-api"
      FIREBASE_SERVICE_ACCOUNT_PATH = var.enable_firebase ? local.firebase_credential_path : ""
    }) : { name = k, value = tostring(v) }]
    secrets = local.api_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-stream-prefix" = "api" })
    }

    # Decides replacement; the ALB check decides routing. More forgiving than
    # the ALB's, so a database blip removes the task from rotation without
    # also killing it.
    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://localhost:8000/health/live || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }

    stopTimeout = 45
  }])

  tags = merge(local.tags, { component = "compute" })
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["python", "-m", "aegis.worker.main", "--concurrency", tostring(var.worker_concurrency)]

    environment = [for k, v in merge(local.common_environment, {
      OTEL_SERVICE_NAME  = "aegis-worker"
      WORKER_CONCURRENCY = tostring(var.worker_concurrency)
    }) : { name = k, value = tostring(v) }]
    secrets = local.worker_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-stream-prefix" = "worker" })
    }

    # 120 s is the Fargate maximum. SIGTERM drains in-flight work; a step
    # still running at the deadline is resumed from its checkpoint.
    stopTimeout = 120
  }])

  tags = merge(local.tags, { component = "compute" })
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name_prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.migrate.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image
    essential = true
    command   = local.migrate_command

    # Only the database. AEGIS_ENV=staging because the production validator
    # would otherwise demand the ingest token, a Gemini key and a Firebase
    # project from a process whose only job is to run SQL files - granting it
    # secrets it never uses would be the worse trade. See the app-change list.
    environment = [
      { name = "AEGIS_ENV", value = "staging" },
      { name = "LOG_FORMAT", value = "json" },
      { name = "POSTGRES_HOST", value = aws_db_instance.this.address },
      { name = "POSTGRES_PORT", value = tostring(aws_db_instance.this.port) },
      { name = "POSTGRES_DB", value = aws_db_instance.this.db_name },
      { name = "POSTGRES_USER", value = aws_db_instance.this.username },
      { name = "OTEL_TRACES_ENABLED", value = "false" },
    ]
    secrets = local.db_password_secret

    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-stream-prefix" = "migrate" })
    }
  }])

  tags = merge(local.tags, { component = "compute" })
}

# ------------------------------------------------------------ services ----
resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count

  # On-demand: interrupting the task that holds every operator's SSE stream to
  # save ~$10/month is a bad trade.
  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Start the new task before stopping the old one. For the ~30 s they overlap
  # there are two API tasks and no Redis, so a browser on the old one may miss
  # live events until it reconnects - it then replays from Postgres by
  # Last-Event-ID. Accepted in exchange for zero-downtime deploys.
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100
  health_check_grace_period_seconds  = 90
  enable_execute_command             = true
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = module.network.task_subnet_ids
    security_groups  = [module.network.task_security_group_id]
    assign_public_ip = module.network.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  tags = merge(local.tags, { component = "compute" })

  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name_prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count

  capacity_provider_strategy {
    capacity_provider = var.worker_use_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
    base              = 0
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Stop the old worker before starting the new one: two workers are safe for
  # the job queue, but would run two heartbeats for the overlap.
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  enable_execute_command             = true
  # Lets worker_use_spot flip between FARGATE_SPOT and FARGATE in place; the
  # provider otherwise replaces the service on a capacity strategy change.
  force_new_deployment = true
  propagate_tags       = "SERVICE"

  network_configuration {
    subnets          = module.network.task_subnet_ids
    security_groups  = [module.network.task_security_group_id]
    assign_public_ip = module.network.assign_public_ip
  }

  tags = merge(local.tags, { component = "compute" })

  lifecycle {
    ignore_changes = [task_definition]
  }
}
