output "execution_role_arn" {
  description = "ECS task execution role. Used by ECS itself, never by application code."
  value       = aws_iam_role.execution.arn
}

output "api_task_role_arn" {
  description = "Task role for the API service."
  value       = aws_iam_role.api.arn
}

output "worker_task_role_arn" {
  description = "Task role for the worker service."
  value       = aws_iam_role.worker.arn
}

output "migrate_task_role_arn" {
  description = "Task role for the one-off migration task."
  value       = aws_iam_role.migrate.arn
}

output "graph_task_role_arn" {
  description = "Task role for the Neo4j task."
  value       = aws_iam_role.graph.arn
}

output "deploy_role_arn" {
  description = "GitHub Actions deploy role. Set this as the AWS_DEPLOY_ROLE_ARN repository variable."
  value       = length(aws_iam_role.deploy) > 0 ? aws_iam_role.deploy[0].arn : ""
}

output "plan_role_arn" {
  description = "GitHub Actions read-only plan role. Set this as the AWS_PLAN_ROLE_ARN repository variable."
  value       = length(aws_iam_role.plan) > 0 ? aws_iam_role.plan[0].arn : ""
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN in use."
  value       = local.oidc_arn
}
