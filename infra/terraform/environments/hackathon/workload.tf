# ----------------------------------------------------------- workload ---
# The system Aegis observes on AWS: gateway -> checkout -> payment, the same
# instrumented image as the local reference chain, as three tiny Fargate Spot
# services in their OWN cluster. A separate cluster is what the ECS runtime
# adapter lists, so Live Systems shows the workload and never Aegis itself.
#
# Aegis's IAM here is read-only (Live Systems, instance state, deployment
# history). Autonomy stays off on AWS, so no write reaches these services.

locals {
  workload_services = {
    gateway  = { downstream = "checkout.${var.service_discovery_namespace}" }
    checkout = { downstream = "payment.${var.service_discovery_namespace}" }
    payment  = { downstream = "" }
  }
  workload_image = "${aws_ecr_repository.workload.repository_url}:${var.workload_image_tag}"
}

resource "aws_ecr_repository" "workload" {
  name                 = "${local.name_prefix}/workload"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.force_destroy_buckets

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}

resource "aws_ecs_cluster" "workload" {
  name = "${local.name_prefix}-workload"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = merge(local.tags, { component = "workload" })
}

resource "aws_ecs_cluster_capacity_providers" "workload" {
  cluster_name       = aws_ecs_cluster.workload.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }
}

resource "aws_service_discovery_service" "workload" {
  for_each = local.workload_services
  name     = each.key

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

resource "aws_vpc_security_group_ingress_rule" "tasks_workload" {
  security_group_id            = module.network.task_security_group_id
  referenced_security_group_id = module.network.task_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "workload HTTP and /metrics between tasks"
  tags                         = local.tags
}

resource "aws_ecs_task_definition" "workload" {
  for_each = local.workload_services

  family                   = "${local.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name         = each.key
    image        = local.workload_image
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment = [
      { name = "SERVICE_NAME", value = each.key },
      { name = "DOWNSTREAM", value = each.value.downstream },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "workload"
      }
    }
  }])

  tags = merge(local.tags, { component = "workload", "aegis.service" = each.key })
}

resource "aws_ecs_service" "workload" {
  for_each = local.workload_services

  name            = each.key
  cluster         = aws_ecs_cluster.workload.id
  task_definition = aws_ecs_task_definition.workload[each.key].arn
  desired_count   = var.workload_desired_count

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100
  propagate_tags                     = "SERVICE"

  network_configuration {
    subnets          = module.network.task_subnet_ids
    security_groups  = [module.network.task_security_group_id]
    assign_public_ip = module.network.assign_public_ip
  }

  service_registries {
    registry_arn = aws_service_discovery_service.workload[each.key].arn
  }

  tags = merge(local.tags, { component = "workload" })

  depends_on = [aws_ecs_cluster_capacity_providers.workload]
}

# Read-only runtime visibility for the API and worker, scoped to the workload
# cluster. No UpdateService, StopTask or RunTask: remediation is off on AWS.
data "aws_iam_policy_document" "workload_read" {
  statement {
    sid       = "ListWorkload"
    actions   = ["ecs:ListServices", "ecs:ListTasks"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.workload.arn]
    }
  }

  statement {
    sid     = "DescribeWorkload"
    actions = ["ecs:DescribeServices", "ecs:DescribeTasks", "ecs:DescribeClusters"]
    resources = [
      aws_ecs_cluster.workload.arn,
      "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.workload.name}/*",
      "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.workload.name}/*",
    ]
  }

  statement {
    sid       = "TaskDefinitions"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "api_workload_read" {
  name   = "workload-read"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.workload_read.json
}

resource "aws_iam_role_policy" "worker_workload_read" {
  name   = "workload-read"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.workload_read.json
}

# Steady synthetic traffic so the workload's request metrics exist. Lives in
# the Aegis cluster, not the workload's, so Live Systems lists only the
# services Aegis observes.
resource "aws_ecs_task_definition" "loadgen" {
  family                   = "${local.name_prefix}-loadgen"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name       = "loadgen"
    image      = "curlimages/curl:8.11.1"
    essential  = true
    entryPoint = ["/bin/sh", "-c"]
    command    = ["while true; do curl -s -o /dev/null -m 5 http://gateway.${var.service_discovery_namespace}:8080/work; sleep 0.4; done"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "loadgen"
      }
    }
  }])

  tags = merge(local.tags, { component = "workload" })
}

resource "aws_ecs_service" "loadgen" {
  name            = "${local.name_prefix}-loadgen"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.loadgen.arn
  desired_count   = var.workload_desired_count > 0 ? 1 : 0

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  network_configuration {
    subnets          = module.network.task_subnet_ids
    security_groups  = [module.network.task_security_group_id]
    assign_public_ip = module.network.assign_public_ip
  }

  tags = merge(local.tags, { component = "workload" })

  depends_on = [aws_ecs_cluster_capacity_providers.this]
}
