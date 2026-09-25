output "queue_url" {
  description = "Investigation queue URL."
  value       = aws_sqs_queue.investigations.url
}

output "queue_arn" {
  description = "Investigation queue ARN, for task role policies."
  value       = aws_sqs_queue.investigations.arn
}

output "queue_name" {
  description = "Investigation queue name. Autoscaling alarms key on this."
  value       = aws_sqs_queue.investigations.name
}

output "dlq_url" {
  description = "Dead-letter queue URL."
  value       = aws_sqs_queue.dlq.url
}

output "dlq_arn" {
  description = "Dead-letter queue ARN."
  value       = aws_sqs_queue.dlq.arn
}

output "dlq_name" {
  description = "Dead-letter queue name. The depth alarm keys on this."
  value       = aws_sqs_queue.dlq.name
}
