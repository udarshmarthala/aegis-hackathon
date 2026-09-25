# The deploy workflow does NOT read these (it never runs Terraform). They are
# for the operator, who copies a handful into GitHub repository variables once
# - see .github/workflows/deploy-hackathon.yml for the list.

output "api_base_url" {
  description = "HTTPS base URL of the API. Set it as NEXT_PUBLIC_API_BASE_URL in Vercel and as HACKATHON_API_BASE_URL in GitHub."
  value       = "https://${aws_cloudfront_distribution.api.domain_name}"
}

output "ecr_repository_url" {
  description = "Backend image repository. GitHub variable HACKATHON_ECR_REPOSITORY takes the name, not the URL."
  value       = aws_ecr_repository.backend.repository_url
}

output "ecr_repository_name" {
  description = "GitHub variable HACKATHON_ECR_REPOSITORY."
  value       = aws_ecr_repository.backend.name
}

output "ecs_cluster_name" {
  description = "GitHub variable HACKATHON_ECS_CLUSTER."
  value       = aws_ecs_cluster.this.name
}

output "api_service_name" {
  description = "API ECS service."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "Worker ECS service."
  value       = aws_ecs_service.worker.name
}

output "github_deploy_role_arn" {
  description = "GitHub variable HACKATHON_AWS_ROLE_ARN."
  value       = try(aws_iam_role.deploy[0].arn, "")
}

output "app_secret_name" {
  description = <<-EOT
    The one secret to populate before the first deploy, as a JSON object whose
    keys are the union of api_secret_keys, worker_secret_keys and (when
    enable_firebase) FIREBASE_SERVICE_ACCOUNT_JSON. ECS refuses to start a
    task whose key is missing.
  EOT
  value       = aws_secretsmanager_secret.app.name
}

output "app_secret_required_keys" {
  description = "Every key the app secret must contain."
  value = sort(distinct(concat(
    var.api_secret_keys,
    var.worker_secret_keys,
    var.enable_firebase ? ["FIREBASE_SERVICE_ACCOUNT_JSON"] : [],
  )))
}

output "artifacts_bucket" {
  description = "Artifact archive bucket."
  value       = aws_s3_bucket.artifacts.bucket
}

output "db_endpoint" {
  description = "RDS endpoint. Private: reachable only from the task security group."
  value       = aws_db_instance.this.address
}

output "log_group_name" {
  description = "CloudWatch log group; streams api/, worker/ and migrate/."
  value       = aws_cloudwatch_log_group.this.name
}

output "public_ipv4_addresses_billed" {
  description = "Public IPv4 addresses this design pays for at $0.005/hour each: one per running task."
  value       = var.api_desired_count + var.worker_desired_count
}
