output "state_bucket" {
  description = "Terraform state bucket. Set as the TF_STATE_BUCKET repository variable and pass with -backend-config."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "State bucket ARN. Pass to each environment root as tf_state_bucket_arn."
  value       = aws_s3_bucket.state.arn
}

output "lock_table" {
  description = "DynamoDB lock table. Set as the TF_LOCK_TABLE repository variable."
  value       = aws_dynamodb_table.locks.name
}

output "lock_table_arn" {
  description = "Lock table ARN. Pass to each environment root as tf_lock_table_arn."
  value       = aws_dynamodb_table.locks.arn
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN. Pass to each environment root as oidc_provider_arn."
  value       = length(aws_iam_openid_connect_provider.github) > 0 ? aws_iam_openid_connect_provider.github[0].arn : ""
}

output "ecr_repository_urls" {
  description = "ECR repository URLs, keyed by repository name. The backend URL is the backend_image input to each environment."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "ecr_repository_names" {
  description = "Repository names. Set as the ECR_REPOSITORY_BACKEND and ECR_REPOSITORY_WEB repository variables."
  value       = [for r in aws_ecr_repository.this : r.name]
}

output "account_id" {
  description = "Account everything was created in."
  value       = data.aws_caller_identity.current.account_id
}
