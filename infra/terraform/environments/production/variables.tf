# Production.
#
# Same modules as staging with different numbers. Where a default differs from
# staging the reason is stated inline, so a reviewer can see what production is
# paying for and why.

variable "aws_region" {
  description = "Region. Aegis is single-region by design."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Owning team. Appears in the tag on every resource."
  type        = string
  default     = "platform"
}

variable "image_tag" {
  description = "Backend image tag to deploy. CI passes the commit SHA; 'bootstrap' is the first-apply placeholder."
  type        = string
  default     = "bootstrap"
}

variable "backend_image" {
  description = "Backend ECR repository URL without a tag, from the bootstrap output ecr_repository_urls."
  type        = string
}

# --------------------------------------------------------------- network ---
variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
  default     = "10.50.0.0/20"
}

variable "nat_strategy" {
  description = <<-EOT
    single, per_az or none_public. See modules/network/variables.tf and
    docs/cost-strategy.md.

    Production defaults to "single": one NAT Gateway, about $33/month, with tasks
    unreachable from the internet. "none_public" removes that cost entirely and
    is a materially weaker posture - it is offered, it is documented, and it is
    not the default.
  EOT
  type        = string
  default     = "single"
}

variable "enable_interface_endpoints" {
  description = "PrivateLink endpoints for AWS services. False: at this traffic volume they cost more than the NAT they would offset."
  type        = bool
  default     = false
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the load balancer. Narrow to office and CI ranges where possible."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate for the HTTPS listener. Required.

    Firebase ID tokens and the alert ingest token cross this boundary on every
    request. Serving production over plain HTTP would put both on the wire in
    the clear, so an empty value is refused rather than defaulted.
  EOT
  type        = string

  validation {
    condition     = length(var.certificate_arn) > 0
    error_message = "certificate_arn is required in production: the API must be served over TLS."
  }
}

# ------------------------------------------------------------- database ---
variable "db_instance_class" {
  description = "RDS instance class. One size up from staging: production holds the real incident history."
  type        = string
  default     = "db.t4g.small"
}

variable "db_multi_az" {
  description = <<-EOT
    Multi-AZ RDS.

    Roughly doubles the instance cost and is the only thing standing between an
    AZ failure and a control plane with no system of record. Default true:
    Aegis degrades gracefully when every other dependency is gone, and not at
    all when Postgres is.
  EOT
  type        = bool
  default     = true
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GiB. Storage autoscaling raises it to the module ceiling as needed."
  type        = number
  default     = 50
}

variable "db_backup_retention_days" {
  description = "Automated backup retention in days. Two weeks of point-in-time recovery."
  type        = number
  default     = 14
}

# ---------------------------------------------------------------- redis ---
variable "enable_redis" {
  description = <<-EOT
    Provision ElastiCache.

    On by default in production, because api_desired_count is 2. With two API
    tasks and no Redis, an SSE event published by one task never reaches a
    browser connected to the other and half the operators watching an incident
    see a stream that silently stops. The validation below refuses that
    combination outright.
  EOT
  type        = bool
  default     = true

  validation {
    # Two API tasks without Redis means an SSE event published by one task
    # never reaches a browser connected to the other, so half the operators
    # watching an incident see a stream that silently stops. That is a
    # correctness failure, not a performance one, so it is refused here.
    condition     = var.enable_redis || var.api_desired_count <= 1
    error_message = "api_desired_count > 1 requires enable_redis = true: SSE fan-out across API replicas goes through Redis pub/sub."
  }
}

# ---------------------------------------------------------------- graph ---
variable "graph_deployment_mode" {
  description = "ecs_fargate, external or disabled. See modules/graph/variables.tf."
  type        = string
  default     = "ecs_fargate"
}

variable "neo4j_external_uri" {
  description = "bolt+s:// URI when graph_deployment_mode is external, for example Neo4j AuraDB."
  type        = string
  default     = ""
}

# -------------------------------------------------------------- compute ---
variable "api_desired_count" {
  description = "API tasks. Two, so replacing one is a rolling deploy rather than an outage."
  type        = number
  default     = 2
}

variable "api_cpu" {
  description = "Fargate CPU units per API task."
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "Fargate memory in MiB per API task."
  type        = number
  default     = 2048
}

variable "worker_min_count" {
  description = <<-EOT
    Minimum worker tasks.

    One, not zero. Scaling from zero costs 90-120 seconds before the first
    investigation starts - a cold-start penalty that is fine in staging and
    lands on a real incident in production. Roughly $15/month on Spot to
    remove it.
  EOT
  type        = number
  default     = 1
}

variable "worker_max_count" {
  description = "Worker ceiling. A hard cap on what an incident storm can cost."
  type        = number
  default     = 8
}

variable "worker_scaling_mode" {
  description = <<-EOT
    sqs_backlog, cpu or fixed.

    "sqs_backlog" is the designed behaviour and the only mode that reaches
    zero. It requires the backend to publish jobs to SQS; until that lands the
    queue stays empty and workers never scale up. Use "cpu" with
    worker_min_count = 1 to run against the current Postgres job queue. See
    docs/aws-architecture.md, "Known gaps".
  EOT
  type        = string
  default     = "sqs_backlog"
}

variable "worker_use_spot" {
  description = "Run workers on FARGATE_SPOT (~70% cheaper). Safe: an interrupted job is re-leased from Postgres."
  type        = bool
  default     = true
}

variable "cpu_architecture" {
  description = "X86_64 or ARM64. ARM64 is ~20% cheaper but requires arm64 images from CI."
  type        = string
  default     = "X86_64"
}

# -------------------------------------------------------- observability ---
variable "log_retention_days" {
  description = "CloudWatch retention in days. Ninety days covers a quarterly audit; never unlimited."
  type        = number
  default     = 90
}

variable "alarm_emails" {
  description = "Addresses subscribed to the alarm topic. Each must confirm by email."
  type        = list(string)
  default     = []
}

variable "budget_limit_usd" {
  description = <<-EOT
    Monthly budget in USD. Zero disables it.

    500 against an estimated steady-state of roughly $275 (docs/cost-strategy.md).
    The headroom is deliberate: the 80% notification should arrive because
    something changed, not because the estimate was optimistic.
  EOT
  type        = number
  default     = 500
}

# ---------------------------------------------------------- application ---
variable "cors_allowed_origins" {
  description = "Exact origins allowed to call the API. No wildcards."
  type        = string
  default     = "http://localhost:3000"
}

variable "api_public_url" {
  description = "Public URL the API is reached on, used in generated links. Defaults to the load balancer."
  type        = string
  default     = ""
}

variable "google_base_url" {
  description = "Gemini's OpenAI-compatible endpoint. Overridable only for a proxy."
  type        = string
  default     = "https://generativelanguage.googleapis.com/v1beta/openai/"
}

variable "llm_model" {
  description = <<-EOT
    The Gemini model used for every task class.

    One model across fast, reasoning and code work is deliberate: failover moves
    between keys, not models, so a fallback changes which quota paid for an
    answer and nothing about the answer itself. Pinned rather than "latest",
    because a prompt or model change is an AI-behaviour change that requires a
    benchmark re-run.
  EOT
  type        = string
  default     = "gemini-3.6-flash"
}

variable "llm_embedding_model" {
  description = "Gemini embedding model. Must emit llm_embedding_dim (1536) dimensions."
  type        = string
  default     = "gemini-embedding-001"
}

variable "google_api_key_count" {
  description = <<-EOT
    How many Google AI Studio keys to provision secret containers for, 1 to 4.

    Redundancy is per key, not per vendor: Gemini quotas are enforced per key
    and per day, so the unit that gets exhausted is the key. Each one becomes
    its own Secrets Manager container that an operator fills separately.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.google_api_key_count >= 1 && var.google_api_key_count <= 4
    error_message = "google_api_key_count must be between 1 and 4."
  }
}

variable "enable_firebase" {
  description = <<-EOT
    Wire the Firebase service account secret into the tasks.

    False leaves the API up with authentication unavailable: it refuses every
    authenticated request rather than accepting them, which is the fail-closed
    behaviour the application already implements.
  EOT
  type        = bool
  default     = true
}

variable "firebase_project_id" {
  description = "Firebase project id. The backend's own config validation requires it in production."
  type        = string
  default     = ""
}

variable "optional_secrets" {
  description = <<-EOT
    Additional secrets to create and inject, named by environment variable.

    Only what is listed here is created, so an integration Aegis reports as
    "not configured" is genuinely absent rather than present with a placeholder
    value - a distinction this platform treats as load bearing (CLAUDE.md
    invariant 6).

    Useful names: GITHUB_TOKEN, SLACK_BOT_TOKEN, SLACK_WEBHOOK_URL,
    LANGSMITH_API_KEY.
  EOT
  type        = list(string)
  default     = []
}

variable "extra_environment_variables" {
  description = "Additional non-secret environment variables for the API and worker."
  type        = map(string)
  default     = {}
}

# --------------------------------------------------------------- github ---
variable "github_repository" {
  description = "owner/repo allowed to assume the deploy and plan roles. Empty creates neither."
  type        = string
  default     = ""
}

variable "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN from the bootstrap output."
  type        = string
  default     = ""
}

variable "tf_state_bucket_arn" {
  description = "State bucket ARN from the bootstrap output."
  type        = string
  default     = ""
}

variable "tf_lock_table_arn" {
  description = "Lock table ARN from the bootstrap output."
  type        = string
  default     = ""
}

variable "observed_cluster_arns" {
  description = "ECS clusters Aegis may observe. Read-only unless allow_remediation_actions is set."
  type        = list(string)
  default     = []
}

variable "allow_remediation_actions" {
  description = "Grant the worker write permissions on observed clusters. Fail closed: default false."
  type        = bool
  default     = false
}
