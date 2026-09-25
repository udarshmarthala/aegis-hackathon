output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "ecs_cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.this.arn
}

output "api_service_name" {
  description = "API ECS service name."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "Worker ECS service name."
  value       = aws_ecs_service.worker.name
}

output "api_task_definition_arn" {
  description = "Latest API task definition revision. The pipeline points the service at this."
  value       = aws_ecs_task_definition.api.arn
}

output "worker_task_definition_arn" {
  description = "Latest worker task definition revision."
  value       = aws_ecs_task_definition.worker.arn
}

output "migrate_task_definition_arn" {
  description = "Migration task definition, run as a one-off task before each rollout."
  value       = aws_ecs_task_definition.migrate.arn
}

output "alb_dns_name" {
  description = "Load balancer DNS name. Point a Route 53 alias or a CNAME at this."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Load balancer hosted zone id, for a Route 53 alias record."
  value       = aws_lb.this.zone_id
}

output "alb_arn_suffix" {
  description = "ALB ARN suffix, the dimension CloudWatch alarms need."
  value       = aws_lb.this.arn_suffix
}

output "target_group_arn_suffix" {
  description = "Target group ARN suffix, for unhealthy-host alarms."
  value       = aws_lb_target_group.api.arn_suffix
}

output "api_base_url" {
  description = <<-EOT
    Base URL for the API - the smoke tests and the frontend both read this.

    HTTPS when a certificate is configured. Plain HTTP only in the explicitly
    opted-in insecure mode. Empty when neither is set, because in that state
    the ALB has no listener and there is no URL to hand out; an empty value is
    a visible failure, where a URL that refuses every connection is not.
  EOT
  value = (
    var.certificate_arn != "" ? "https://${aws_lb.this.dns_name}" :
    var.allow_insecure_http ? "http://${aws_lb.this.dns_name}" : ""
  )
}

output "log_group_name" {
  description = "CloudWatch log group all Aegis containers write to."
  value       = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  description = "Log group ARN, for task role policies."
  value       = aws_cloudwatch_log_group.this.arn
}
