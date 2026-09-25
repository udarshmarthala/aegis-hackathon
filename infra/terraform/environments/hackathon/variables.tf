# Hackathon environment. Least cost that still serves the demo well: one API
# task, one worker, a micro Postgres, no NAT, no Redis, no Neo4j, and CloudFront
# as the HTTPS front door so no domain or certificate is needed.
#
# docs/AWS_HACKATHON_ARCHITECTURE.md explains every choice below;
# docs/AWS_COST_ANALYSIS.md prices them.

variable "aws_region" {
  description = "Region. us-west-2 because the Bedrock inference profiles Aegis uses were verified there."
  type        = string
  default     = "us-west-2"
}

variable "owner" {
  description = "Owning person or team. Appears in the tag on every resource."
  type        = string
  default     = "hackathon"
}

variable "image_tag" {
  description = <<-EOT
    Backend image tag for the task definitions Terraform registers.

    The deploy workflow owns the running revision after the first deploy (it
    registers new revisions from the latest one with a new image and rolls the
    services). Pass the tag that is currently deployed when re-applying, or the
    next apply registers a revision pointing at an older image - harmless,
    because the services ignore task_definition, but confusing.
    "bootstrap" is the first-apply placeholder: no such image exists, so apply
    the first time with api_desired_count = worker_desired_count = 0 and let
    the first workflow run (bring_up = true) start the services.
  EOT
  type        = string
  default     = "bootstrap"
}

variable "cpu_architecture" {
  description = <<-EOT
    ARM64 or X86_64. ARM64 is 20% cheaper per vCPU-hour and the backend image
    builds cleanly for linux/arm64. It MUST match the platform the deploy
    workflow builds (its `platform` input); a mismatch is an exec format error
    at task start.
  EOT
  type        = string
  default     = "ARM64"

  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
}

# --------------------------------------------------------------- network ---
variable "vpc_cidr" {
  description = "VPC CIDR. A /20 split into /24s by the shared network module."
  type        = string
  default     = "10.60.0.0/20"
}

# -------------------------------------------------------------- compute ---
variable "api_cpu" {
  description = "Fargate CPU units for the API task. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory in MiB for the API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = <<-EOT
    API tasks. One, and it must stay one while there is no Redis: SSE events
    published by one API task never reach a browser connected to another.
    Zero is allowed so the environment can be parked (see the shutdown
    procedure in docs/AWS_HACKATHON_ARCHITECTURE.md).
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.api_desired_count >= 0 && var.api_desired_count <= 1
    error_message = "api_desired_count must be 0 or 1: there is no Redis to fan SSE out across API replicas."
  }
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker. Investigations wait on model calls, not on CPU."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Fargate memory in MiB for the worker."
  type        = number
  default     = 1024
}

variable "worker_desired_count" {
  description = <<-EOT
    Worker tasks. One always-on worker polling the Postgres job queue; it also
    hosts the heartbeat and metrics forwarder, which must not scale to zero.
    Two or more are safe for the job queue (FOR UPDATE SKIP LOCKED) but run a
    heartbeat each.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_count >= 0 && var.worker_desired_count <= 3
    error_message = "worker_desired_count must be between 0 and 3."
  }
}

variable "worker_use_spot" {
  description = <<-EOT
    Run the worker on FARGATE_SPOT (about 70% cheaper).

    Safe for correctness - jobs are Postgres rows and every step is
    checkpointed - but NOT free of latency: an interrupted worker gets a new
    hostname, so its in-flight job waits for the 15-minute stale-job reaper
    before another worker resumes it. Set false for the judging window.
  EOT
  type        = bool
  default     = true
}

variable "worker_concurrency" {
  description = "Investigations one worker runs at once."
  type        = number
  default     = 2
}

variable "log_retention_days" {
  description = "CloudWatch retention. Short: this is a demo, and infinite retention is refused."
  type        = number
  default     = 7

  validation {
    condition     = var.log_retention_days > 0
    error_message = "log_retention_days must be greater than zero; zero means keep forever."
  }
}

# ------------------------------------------------------------- database ---
variable "db_instance_class" {
  description = "RDS instance class. Graviton micro: the system of record for a demo, not a fleet."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = <<-EOT
    PostgreSQL version. Major only, so RDS picks its current default minor:
    pinning a minor (the other roots pin 16.4) breaks the day RDS retires it,
    and 16.4 was no longer orderable in us-west-2 when this was written.
  EOT
  type        = string
  default     = "16"
}

variable "db_allocated_storage" {
  description = "gp3 storage in GiB. 20 is the gp3 minimum on RDS PostgreSQL."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "Automated backups. Free up to the size of the database; never 0 (that disables PITR)."
  type        = number
  default     = 3

  validation {
    condition     = var.db_backup_retention_days >= 1
    error_message = "db_backup_retention_days must be at least 1."
  }
}

variable "db_deletion_protection" {
  description = "Refuse to delete the database. Off: this environment is meant to be destroyed after the event."
  type        = bool
  default     = false
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Set false if the demo data is worth keeping."
  type        = bool
  default     = true
}

variable "force_destroy_buckets" {
  description = "Let terraform destroy empty and delete the artifacts bucket."
  type        = bool
  default     = true
}

# ---------------------------------------------------------- application ---
variable "aegis_env" {
  description = <<-EOT
    AEGIS_ENV for the API and worker. "production" turns on the backend's
    hardening validators: no dev-auth bypass, no wildcard CORS, and an ingest
    token, Firebase project id, Postgres password and at least one Gemini key
    must all be present or the process refuses to start. "staging" relaxes
    those, and is the fallback if no Gemini key is available.
  EOT
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "staging"], var.aegis_env)
    error_message = "aegis_env must be production or staging on AWS; local enables demo controls meant for a laptop."
  }
}

variable "cors_allowed_origins" {
  description = <<-EOT
    Exact origins allowed to call the API, comma-separated - the Vercel
    production URL, e.g. "https://aegis-hackathon.vercel.app". No wildcards:
    the production validator refuses them.
  EOT
  type        = string

  validation {
    condition     = !strcontains(var.cors_allowed_origins, "*") && length(trimspace(var.cors_allowed_origins)) > 0
    error_message = "cors_allowed_origins must list exact origins; wildcards are refused."
  }
}

variable "firebase_project_id" {
  description = "Firebase project id. Required by the production validator."
  type        = string
  default     = ""
}

variable "enable_firebase" {
  description = <<-EOT
    Inject the Firebase service account into the API task. The JSON lives in
    the app secret under the key FIREBASE_SERVICE_ACCOUNT_JSON and is written
    to a 0600 file at task start (the backend reads a file path). False leaves
    the API up and refusing every authenticated request.
  EOT
  type        = bool
  default     = true
}

variable "bedrock_model_id" {
  description = "Primary brain model: a US cross-region inference profile id."
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "bedrock_fallback_model_id" {
  description = "Used when the primary is refused (403) - verified working in this account."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "bedrock_inference_regions" {
  description = <<-EOT
    Regions a "us." inference profile may route to. IAM must allow the
    foundation model in each of them, or a request routed there fails with
    AccessDenied at random. From `aws bedrock list-inference-profiles`.
  EOT
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}

variable "api_secret_keys" {
  description = <<-EOT
    Keys of the JSON app secret injected into the API task.

    Every key listed MUST exist in the secret value, or ECS refuses to start
    the task - the intended fail-closed behaviour. For an integration you do
    not have, remove its key from this list rather than storing an empty
    value, so it is reported as "not configured".

    The sponsor keys are here because the war-room badges report the API
    process's own configuration; see the app-change list in
    docs/AWS_HACKATHON_ARCHITECTURE.md for the change that removes them.
  EOT
  type        = list(string)
  default = [
    "ALERT_INGEST_TOKEN",
    "GOOGLE_API_KEY",
    "RAWTREE_READ_KEY",
    "NIMBLE_API_KEY",
    "BFL_API_KEY",
  ]
}

variable "worker_secret_keys" {
  description = <<-EOT
    Keys of the JSON app secret injected into the worker task. Same rules as
    api_secret_keys. ALERT_INGEST_TOKEN is here only because the production
    validator runs in every process.
  EOT
  type        = list(string)
  default = [
    "ALERT_INGEST_TOKEN",
    "GOOGLE_API_KEY",
    "RAWTREE_WRITE_KEY",
    "RAWTREE_READ_KEY",
    "NIMBLE_API_KEY",
    "BFL_API_KEY",
  ]
}

variable "neo4j_uri" {
  description = <<-EOT
    Optional external Neo4j (e.g. AuraDB Free: neo4j+s://xxxx.databases.neo4j.io).
    Empty runs without topology: every graph tool reports "source unavailable"
    and the investigation records an evidence gap. When set, add NEO4J_PASSWORD
    to worker_secret_keys and api_secret_keys.
  EOT
  type        = string
  default     = ""
}

variable "extra_environment_variables" {
  description = "Additional non-secret environment variables for the API and worker."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------ edge ---
variable "cloudfront_price_class" {
  description = "PriceClass_100 (North America and Europe edges). Cost is inside the free tier either way."
  type        = string
  default     = "PriceClass_100"
}

variable "origin_read_timeout" {
  description = <<-EOT
    CloudFront origin response timeout in seconds - also the longest silence
    allowed BETWEEN packets of a streamed response. The SSE endpoints send a
    heartbeat at least every 15 s, so 60 leaves 4x headroom. 60 is the maximum
    without a quota increase.
  EOT
  type        = number
  default     = 60
}

# ---------------------------------------------------------- github oidc ---
variable "github_repository" {
  description = "owner/repo whose deploy workflow may assume the deploy role. Empty creates no role."
  type        = string
  default     = "udarshmarthala/aegis-hackathon"
}

variable "github_environment" {
  description = "GitHub Environment the deploy job runs in. The role trusts ONLY this environment's token subject."
  type        = string
  default     = "aegis-hackathon"
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Create the account's GitHub OIDC provider. The account has none today.
    It is an account-level singleton: if infra/terraform/bootstrap is applied
    later, set this false here and pass its ARN as github_oidc_provider_arn.
  EOT
  type        = bool
  default     = true
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN, used when create_github_oidc_provider is false."
  type        = string
  default     = ""
}

# --------------------------------------------------------------- budget ---
variable "budget_limit_usd" {
  description = "Monthly AWS Budget for this environment. Zero disables it."
  type        = number
  default     = 100
}

variable "budget_emails" {
  description = "Addresses notified at 50/80/100% (forecast) of the budget."
  type        = list(string)
  default     = []
}

variable "github_immutable_sub_prefix" {
  description = "Immutable OIDC subject prefix, e.g. repo:owner@<owner_id>/repo@<repo_id> (from GET /repos/{repo}/actions/oidc/customization/sub). Empty when the repo uses classic subjects."
  type        = string
  default     = ""
}

variable "service_discovery_namespace" {
  description = "Private DNS namespace the support tier registers in."
  type        = string
  default     = "aegis.internal"
}

variable "obs_desired_count" {
  description = "Tasks for the Redis/Neo4j/Prometheus/Tempo/Loki support tier (0 turns it off)."
  type        = number
  default     = 1
}

variable "workload_image_tag" {
  description = "Tag of the workload image (gateway/checkout/payment) in the workload ECR repository."
  type        = string
  default     = "1.4.1"
}

variable "workload_desired_count" {
  description = "Tasks per workload service (0 stops the observed workload)."
  type        = number
  default     = 1
}
