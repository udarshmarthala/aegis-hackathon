variable "name_prefix" {
  description = "Prefix for every resource name, e.g. aegis-staging."
  type        = string
}

variable "environment" {
  description = "Environment name: staging or production."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the DB subnet group and cache subnet group."
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group that permits access from the task security group only."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key for RDS and secret encryption."
  type        = string
}

# ------------------------------------------------------------ postgres -----
variable "engine_version" {
  description = "PostgreSQL major.minor. 16 is required: the schema uses pgvector, available on RDS PostgreSQL 16."
  type        = string
  default     = "16.4"
}

variable "instance_class" {
  description = <<-EOT
    RDS instance class. Graviton (t4g) is ~10% cheaper than the equivalent t3
    for identical vCPU and memory, and PostgreSQL has first-class arm64
    support, so there is no reason to pay for x86 here.

    db.t4g.micro (2 vCPU burst, 1GiB) is enough for staging. Production starts
    at db.t4g.small (2GiB) - see docs/cost-strategy.md for why Aurora
    Serverless v2 was rejected.
  EOT
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Initial gp3 storage in GiB."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = <<-EOT
    Storage autoscaling ceiling in GiB. A ceiling, not a target: it exists so a
    runaway evidence write cannot silently grow the bill without limit, while
    still preventing a full disk from taking the system of record offline.
  EOT
  type        = number
  default     = 100
}

variable "multi_az" {
  description = "Multi-AZ RDS. Roughly doubles the instance cost in exchange for automatic failover."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention. Never 0: that disables point-in-time recovery entirely."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 1
    error_message = "backup_retention_days must be at least 1; 0 disables point-in-time recovery."
  }
}

variable "deletion_protection" {
  description = "Refuse to delete the instance. True in production, always."
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Only ever acceptable in a throwaway environment."
  type        = bool
  default     = false
}

variable "performance_insights_enabled" {
  description = "Performance Insights. Free for 7 days of retention; longer retention is billed."
  type        = bool
  default     = true
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "aegis"
}

variable "db_username" {
  description = "Master username. The password is generated and stored by RDS in Secrets Manager; it never enters Terraform state."
  type        = string
  default     = "aegis"
}

variable "monitoring_interval" {
  description = "Enhanced Monitoring interval in seconds. 0 disables it (it is billed per instance per month)."
  type        = number
  default     = 0
}

# ------------------------------------------------------------- redis -------
variable "enable_redis" {
  description = <<-EOT
    Provision ElastiCache for Redis.

    Default false. Redis in Aegis is a cache and an SSE pub/sub bus, never a
    system of record (CLAUDE.md invariant 10), and the API degrades to direct
    reads when it is absent. With a single API task there is nothing to fan
    events out TO, so the ~$12/month buys nothing.

    Turn this on when api_desired_count > 1: without it, an SSE event published
    by task A never reaches a browser connected to task B, and the incident
    stream silently goes stale for half of the operators watching it. The
    staging and production roots assert this pairing.
  EOT
  type        = bool
  default     = false
}

variable "redis_node_type" {
  description = "ElastiCache node type when enabled."
  type        = string
  default     = "cache.t4g.micro"
}

# ---------------------------------------------------------- s3 lifecycle ---
variable "artifact_retention" {
  description = <<-EOT
    Days before each artifact class transitions to infrequent access and then
    expires. Keys are S3 prefixes; the application writes under these.

    Evaluation artifacts outlive everything else because a benchmark result you
    cannot reproduce is not a benchmark result.
  EOT
  type = map(object({
    transition_ia_days      = number
    transition_glacier_days = number
    expiration_days         = number
  }))
  default = {
    "investigations/" = { transition_ia_days = 30, transition_glacier_days = 90, expiration_days = 365 }
    "evidence/"       = { transition_ia_days = 30, transition_glacier_days = 120, expiration_days = 730 }
    "executions/"     = { transition_ia_days = 30, transition_glacier_days = 90, expiration_days = 365 }
    "evaluations/"    = { transition_ia_days = 60, transition_glacier_days = 180, expiration_days = 1095 }
  }
}

variable "alb_log_retention_days" {
  description = "Days to keep ALB access logs in S3."
  type        = number
  default     = 90
}

variable "force_destroy_buckets" {
  description = "Allow terraform destroy to delete non-empty buckets. Staging convenience; never production."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
