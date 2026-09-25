# Neo4j, the operational knowledge graph.
#
# Single task, EFS-backed, private DNS only. See the deployment_mode variable
# for why a single non-HA task is the right level of investment for topology
# data that Aegis is built to survive the loss of.

locals {
  self    = var.deployment_mode == "ecs_fargate"
  tags    = merge(var.tags, { component = "graph" })
  service = "${var.name_prefix}-neo4j"
  # Cloud Map gives the task a stable name even though its IP changes on every
  # restart. Without it the API would need to rediscover the task IP itself.
  namespace = "${var.name_prefix}.internal"
}

# ---------------------------------------------------- persistent storage ---
# EFS rather than an ephemeral volume: a Fargate task that is replaced for any
# reason (a deploy, a platform update, an AZ event) would otherwise come back
# with an empty graph and every topology query would return "no evidence
# found" - which Aegis is required to distinguish from "source unavailable",
# and it would report the wrong one.
resource "aws_efs_file_system" "this" {
  count = local.self ? 1 : 0

  creation_token = "${var.name_prefix}-neo4j"
  encrypted      = true

  # The graph is small and read constantly. Bursting throughput with elastic
  # sizing costs nothing extra at this volume; provisioned throughput would.
  throughput_mode  = "bursting"
  performance_mode = "generalPurpose"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-neo4j-data" })
}

resource "aws_efs_mount_target" "this" {
  count = local.self ? length(var.subnet_ids) : 0

  file_system_id  = aws_efs_file_system.this[0].id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [var.efs_security_group_id]
}

# An access point pins ownership to the uid Neo4j runs as (7474), so the
# container does not need root to write to its own data directory.
resource "aws_efs_access_point" "this" {
  count = local.self ? 1 : 0

  file_system_id = aws_efs_file_system.this[0].id

  posix_user {
    uid = 7474
    gid = 7474
  }

  root_directory {
    path = "/neo4j"

    creation_info {
      owner_uid   = 7474
      owner_gid   = 7474
      permissions = "0750"
    }
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-neo4j-ap" })
}

resource "aws_efs_file_system_policy" "this" {
  count = local.self ? 1 : 0

  file_system_id = aws_efs_file_system.this[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = { AWS = "*" }
      Action    = "*"
      Resource  = aws_efs_file_system.this[0].arn
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

# ------------------------------------------------------ service discovery ---
resource "aws_service_discovery_private_dns_namespace" "this" {
  count = local.self ? 1 : 0

  name        = local.namespace
  description = "Internal DNS for Aegis in-VPC services."
  vpc         = var.vpc_id
  tags        = local.tags
}

resource "aws_service_discovery_service" "neo4j" {
  count = local.self ? 1 : 0

  name = "neo4j"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this[0].id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # Cloud Map's own health checking is enough here; an ALB in front of a
  # single internal graph task would be pure cost.
  health_check_custom_config {
    failure_threshold = 1
  }

  tags = local.tags
}

# ---------------------------------------------------------- ecs service ----
resource "aws_ecs_task_definition" "neo4j" {
  count = local.self ? 1 : 0

  family                   = local.service
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  volume {
    name = "neo4j-data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.this[0].id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.this[0].id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "neo4j"
    image     = var.image
    essential = true

    portMappings = [
      { containerPort = 7687, protocol = "tcp" },
      { containerPort = 7474, protocol = "tcp" },
    ]

    environment = [
      { name = "NEO4J_server_memory_heap_max__size", value = var.heap_max_size },
      { name = "NEO4J_server_memory_heap_initial__size", value = var.heap_max_size },
      { name = "NEO4J_server_memory_pagecache_size", value = var.pagecache_size },
      # Bind to every interface: the awsvpc ENI address is not knowable here.
      { name = "NEO4J_server_default__listen__address", value = "0.0.0.0" },
      { name = "NEO4J_server_bolt_listen__address", value = "0.0.0.0:7687" },
    ]

    # The password arrives from Secrets Manager. NEO4J_AUTH expects
    # "neo4j/<password>", so the secret must hold that whole string rather than
    # the password alone - a sharp edge worth stating where the operator will
    # look for it. docs/terraform.md repeats it.
    secrets = [
      { name = "NEO4J_AUTH", valueFrom = var.password_secret_arn },
    ]

    mountPoints = [{
      sourceVolume  = "neo4j-data"
      containerPath = "/data"
      readOnly      = false
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "neo4j"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "wget -qO- http://localhost:7474 >/dev/null 2>&1 || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 5
      startPeriod = 90
    }

    stopTimeout = 60
  }])

  tags = merge(local.tags, { Name = local.service })
}

resource "aws_ecs_service" "neo4j" {
  count = local.self ? 1 : 0

  name            = local.service
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.neo4j[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # A single writer on a single EFS directory. Two Neo4j tasks pointed at the
  # same store would corrupt it, so the deployment must never overlap:
  # maximum_percent 100 stops the old task before starting the new one.
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0

  enable_execute_command = true
  propagate_tags         = "SERVICE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.task_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  service_registries {
    registry_arn = aws_service_discovery_service.neo4j[0].arn
  }

  tags = merge(local.tags, { Name = local.service })

  depends_on = [aws_efs_mount_target.this]
}
