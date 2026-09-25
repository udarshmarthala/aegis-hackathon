output "db_instance_id" {
  description = "RDS instance identifier, for alarms and CLI operations."
  value       = aws_db_instance.this.identifier
}

output "db_address" {
  description = "RDS endpoint hostname."
  value       = aws_db_instance.this.address
}

output "db_port" {
  description = "RDS port."
  value       = aws_db_instance.this.port
}

output "db_name" {
  description = "Initial database name."
  value       = aws_db_instance.this.db_name
}

output "db_username" {
  description = "Master username."
  value       = aws_db_instance.this.username
}

output "db_master_secret_arn" {
  description = <<-EOT
    ARN of the RDS-managed master credential secret. The secret's value is JSON
    with username and password keys; an ECS task definition selects the field
    with the "<arn>:password::" valueFrom syntax.
  EOT
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "artifacts_bucket_name" {
  description = "Bucket holding investigation, evidence, execution and evaluation artifacts."
  value       = aws_s3_bucket.artifacts.bucket
}

output "artifacts_bucket_arn" {
  description = "Artifacts bucket ARN, for task role policies."
  value       = aws_s3_bucket.artifacts.arn
}

output "alb_logs_bucket_name" {
  description = "Bucket the load balancer writes access logs to."
  value       = aws_s3_bucket.alb_logs.bucket
}

output "redis_enabled" {
  description = "Whether ElastiCache was provisioned."
  value       = var.enable_redis
}

output "redis_primary_endpoint" {
  description = "Redis primary endpoint, or an empty string when Redis is disabled."
  value       = var.enable_redis ? aws_elasticache_replication_group.this[0].primary_endpoint_address : ""
}
