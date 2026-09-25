output "sns_topic_arn" {
  description = "Alarm topic. Subscribe a chat webhook here as well as email."
  value       = aws_sns_topic.alarms.arn
}

output "alarm_names" {
  description = "Every alarm created, for the post-deployment verification step."
  value = compact(concat(
    [for a in aws_cloudwatch_metric_alarm.alb_5xx : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.target_5xx : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.unhealthy_hosts : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.latency : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.db_cpu : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.db_storage : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.db_connections : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.api_cpu : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.api_memory : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.worker_memory : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.dead_letters : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.queue_stalled : a.alarm_name],
  ))
}

output "budget_name" {
  description = "Monthly budget name, or an empty string when no budget is configured."
  value       = var.budget_limit_usd > 0 ? aws_budgets_budget.monthly[0].name : ""
}
