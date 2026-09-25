variable "name_prefix" {
  description = "Prefix for every resource name, e.g. aegis-staging."
  type        = string
}

variable "environment" {
  description = "Environment name: staging or production."
  type        = string
}

variable "alarm_emails" {
  description = <<-EOT
    Addresses subscribed to the alarm topic. Each recipient must confirm the
    subscription by email; an unconfirmed subscription receives nothing, which
    is the usual reason "the alarms never fired".
  EOT
  type        = list(string)
  default     = []
}

variable "budget_limit_usd" {
  description = <<-EOT
    Monthly budget in USD. Zero disables the budget entirely.

    A budget is not a spend limit - AWS will not stop anything - but it is the
    difference between noticing a runaway cost on day two and on the invoice.
  EOT
  type        = number
  default     = 0
}

variable "budget_thresholds_percent" {
  description = "Percentages of the budget at which to notify. The 100% entry is forecast-based; the rest are actual spend."
  type        = list(number)
  default     = [50, 80, 100]
}

# --------------------------------------------------------- alarm targets ---
# Passed as plain identifiers rather than resource references so this module
# can be applied alongside the resources it watches without a dependency cycle.
variable "alb_arn_suffix" {
  description = "ALB ARN suffix. Empty disables the load balancer alarms."
  type        = string
  default     = ""
}

variable "target_group_arn_suffix" {
  description = "Target group ARN suffix. Empty disables the unhealthy-host alarm."
  type        = string
  default     = ""
}

variable "db_instance_id" {
  description = "RDS instance identifier. Empty disables the database alarms."
  type        = string
  default     = ""
}

variable "db_storage_alarm_bytes" {
  description = "Free storage floor before alarming. 2 GiB by default."
  type        = number
  default     = 2147483648
}

variable "db_connection_alarm_threshold" {
  description = "Connection count that indicates the pool is leaking or the ceiling is too low."
  type        = number
  default     = 80
}

variable "ecs_cluster_name" {
  description = "ECS cluster name. Empty disables the service alarms."
  type        = string
  default     = ""
}

variable "api_service_name" {
  description = "API service name, for its CPU, memory and task-count alarms."
  type        = string
  default     = ""
}

variable "worker_service_name" {
  description = "Worker service name."
  type        = string
  default     = ""
}

variable "dlq_name" {
  description = "Dead-letter queue name. Empty disables the dead-letter alarm."
  type        = string
  default     = ""
}

variable "queue_name" {
  description = "Investigation queue name, for the stalled-queue alarm."
  type        = string
  default     = ""
}

variable "queue_age_alarm_seconds" {
  description = <<-EOT
    Age of the oldest queued message that means work is stuck.

    900s is three times the expected end-to-end investigation time. Below that
    this alarm fires every time an incident storm queues legitimately.
  EOT
  type        = number
  default     = 900
}

variable "target_response_time_seconds" {
  description = "p95 API latency that counts as degraded."
  type        = number
  default     = 3
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
