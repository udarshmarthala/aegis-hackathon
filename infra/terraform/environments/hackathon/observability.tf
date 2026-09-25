# ------------------------------------------------------ observability ---
# Redis, Neo4j, Prometheus, Tempo and Loki as sidecars of ONE small Fargate
# Spot task, found by the API and worker at obs.aegis.internal.
#
# Why one task: each is tiny at demo load, and five services would mean five
# task minimums, five ENIs and five sets of logs for what is one private
# support tier. Why Spot: every one of them is soft by design (CLAUDE.md
# invariants 9-10) - Redis is never authoritative, Neo4j is a projection of
# topology, and Prometheus/Tempo/Loki keep hours of history, not records. A
# Spot interruption loses that history and nothing else; the app records the
# gap while the task is replaced.
#
# Storage is the task's ephemeral disk. Durable state lives in RDS and S3.

locals {
  obs_host = "obs.${var.service_discovery_namespace}"

  tempo_config = <<-YAML
    server:
      http_listen_port: 3200
    distributor:
      receivers:
        otlp:
          protocols:
            grpc:
              endpoint: 0.0.0.0:4317
    storage:
      trace:
        backend: local
        local:
          path: /tmp/tempo/traces
        wal:
          path: /tmp/tempo/wal
    compactor:
      compaction:
        block_retention: 24h
  YAML

  prometheus_config = <<-YAML
    global:
      scrape_interval: 15s
    scrape_configs:
      - job_name: prometheus
        static_configs:
          - targets: ["localhost:9090"]
            labels: { service: prometheus }
      - job_name: tempo
        static_configs:
          - targets: ["localhost:3200"]
            labels: { service: tempo }
      - job_name: loki
        static_configs:
          - targets: ["localhost:3100"]
            labels: { service: loki }
  YAML

  obs_log = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.this.name
      "awslogs-region"        = var.aws_region
      "awslogs-stream-prefix" = "obs"
    }
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = var.service_discovery_namespace
  description = "Private service discovery for the Aegis support tier"
  vpc         = module.network.vpc_id
  tags        = local.tags
}

resource "aws_service_discovery_service" "obs" {
  name = "obs"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this.id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = local.tags
}

# Task-to-task only: the support tier is reachable from the API and worker's
# own security group and from nothing else.
resource "aws_vpc_security_group_ingress_rule" "tasks_obs" {
  for_each = {
    redis      = 6379
    prometheus = 9090
    tempo      = 3200
    otlp       = 4317
    loki       = 3100
  }

  security_group_id            = module.network.task_security_group_id
  referenced_security_group_id = module.network.task_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "${each.key} from app tasks"
  tags                         = local.tags
}

resource "aws_ecs_task_definition" "obs" {
  family                   = "${local.name_prefix}-obs"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 3072
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name              = "redis"
      image             = "redis:7.4-alpine"
      essential         = true
      memoryReservation = 128
      # No password, so bind protection must be lifted explicitly; the
      # security group is the boundary, and Redis holds nothing authoritative.
      command          = ["redis-server", "--protected-mode", "no", "--save", "", "--appendonly", "no"]
      portMappings     = [{ containerPort = 6379, protocol = "tcp" }]
      logConfiguration = local.obs_log
    },
    {
      name              = "neo4j"
      image             = "neo4j:5.26-community"
      essential         = true
      memoryReservation = 1024
      portMappings      = [{ containerPort = 7687, protocol = "tcp" }]
      environment = [
        # Auth off inside a private, security-group-bounded network: Neo4j
        # here is a rebuildable projection of topology, not a record.
        { name = "NEO4J_AUTH", value = "none" },
        { name = "NEO4J_server_memory_heap_initial__size", value = "256m" },
        { name = "NEO4J_server_memory_heap_max__size", value = "512m" },
        { name = "NEO4J_server_memory_pagecache_size", value = "256m" },
      ]
      logConfiguration = local.obs_log
    },
    {
      name              = "prometheus"
      image             = "prom/prometheus:v3.0.1"
      essential         = true
      memoryReservation = 256
      entryPoint        = ["/bin/sh", "-c"]
      command = [
        "printf '%s' \"$PROM_CONFIG\" > /tmp/prometheus.yml && exec /bin/prometheus --config.file=/tmp/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=24h --web.listen-address=:9090"
      ]
      environment      = [{ name = "PROM_CONFIG", value = local.prometheus_config }]
      portMappings     = [{ containerPort = 9090, protocol = "tcp" }]
      logConfiguration = local.obs_log
    },
    {
      name              = "tempo"
      image             = "grafana/tempo:2.6.1"
      essential         = true
      memoryReservation = 256
      entryPoint        = ["/bin/sh", "-c"]
      command = [
        "mkdir -p /tmp/tempo && printf '%s' \"$TEMPO_CONFIG\" > /tmp/tempo.yaml && exec /tempo -config.file=/tmp/tempo.yaml"
      ]
      environment = [{ name = "TEMPO_CONFIG", value = local.tempo_config }]
      portMappings = [
        { containerPort = 3200, protocol = "tcp" },
        { containerPort = 4317, protocol = "tcp" },
      ]
      logConfiguration = local.obs_log
    },
    {
      name              = "loki"
      image             = "grafana/loki:3.3.2"
      essential         = true
      memoryReservation = 256
      # The image's own single-binary, filesystem-backed config. Its gRPC port
      # moves off 9095: sidecars share one network namespace and Tempo's gRPC
      # server already holds it.
      command          = ["-config.file=/etc/loki/local-config.yaml", "-server.grpc-listen-port=9096"]
      portMappings     = [{ containerPort = 3100, protocol = "tcp" }]
      logConfiguration = local.obs_log
    },
  ])

  tags = merge(local.tags, { component = "observability" })
}

resource "aws_ecs_service" "obs" {
  name            = "${local.name_prefix}-obs"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.obs.arn
  desired_count   = var.obs_desired_count

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }

  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = module.network.task_subnet_ids
    security_groups  = [module.network.task_security_group_id]
    assign_public_ip = module.network.assign_public_ip
  }

  service_registries {
    registry_arn = aws_service_discovery_service.obs.arn
  }

  tags = merge(local.tags, { component = "observability" })

  depends_on = [aws_ecs_cluster_capacity_providers.this]
}
