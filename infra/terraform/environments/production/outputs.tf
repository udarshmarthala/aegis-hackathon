# These names are a contract with .github/workflows/deploy-production.yml, which
# reads them with `terraform output -json`. Renaming one breaks the deploy.

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = module.compute.ecs_cluster_name
}

output "api_service_name" {
  description = "API ECS service name."
  value       = module.compute.api_service_name
}

output "worker_service_name" {
  description = "Worker ECS service name."
  value       = module.compute.worker_service_name
}

output "api_task_definition_arn" {
  description = "Task definition revision the pipeline rolls the API service onto."
  value       = module.compute.api_task_definition_arn
}

output "worker_task_definition_arn" {
  description = "Task definition revision the pipeline rolls the worker service onto."
  value       = module.compute.worker_task_definition_arn
}

output "migrate_task_definition_arn" {
  description = "One-off migration task definition."
  value       = module.compute.migrate_task_definition_arn
}

output "task_subnet_ids" {
  description = "Subnets the one-off migration task runs in."
  value       = module.network.task_subnet_ids
}

output "task_security_group_id" {
  description = "Security group for the one-off migration task."
  value       = module.network.task_security_group_id
}

output "log_group_name" {
  description = "Log group the deploy job tails the migration task from."
  value       = module.compute.log_group_name
}

output "api_base_url" {
  description = "Base URL the smoke tests run against."
  value       = module.compute.api_base_url
}

# ------------------------------------------------- operator-facing values --
output "alb_dns_name" {
  description = "Point the Vercel frontend and any DNS record at this."
  value       = module.compute.alb_dns_name
}

output "artifacts_bucket" {
  description = "Artifact bucket name."
  value       = module.data.artifacts_bucket_name
}

output "queue_url" {
  description = "Investigation queue URL."
  value       = module.queue.queue_url
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable."
  value       = module.iam.deploy_role_arn
}

output "github_plan_role_arn" {
  description = "Set as the AWS_PLAN_ROLE_ARN repository variable."
  value       = module.iam.plan_role_arn
}

output "secret_arns_to_populate" {
  description = <<-EOT
    Secret containers Terraform created with no value. Every one must be
    populated with `aws secretsmanager put-secret-value` before the first
    deploy: ECS cannot start a task whose secret does not resolve, which is the
    intended fail-closed behaviour.
  EOT
  value = concat(
    [for s in aws_secretsmanager_secret.core : s.name],
    var.graph_deployment_mode == "disabled" ? [] : [aws_secretsmanager_secret.neo4j[0].name],
    var.enable_firebase ? [aws_secretsmanager_secret.firebase[0].name] : [],
  )
}

output "nat_gateway_count" {
  description = "NAT Gateways created. Each is roughly $33/month before data processing."
  value       = module.network.nat_gateway_count
}
