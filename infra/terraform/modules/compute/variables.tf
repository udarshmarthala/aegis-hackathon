variable "name_prefix" {
  description = "Prefix for every resource name, e.g. aegis-staging."
  type        = string
}

variable "environment" {
  description = "Environment name: staging or production."
  type        = string
}

variable "aws_region" {
  description = "Region, for the awslogs driver configuration."
  type        = string
}

# --------------------------------------------------------------- network ---
variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "public_subnet_ids" {
  description = "Subnets for the load balancer."
  type        = list(string)
}

variable "task_subnet_ids" {
  description = "Subnets for ECS tasks."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Give tasks a public IP. True only when nat_strategy is none_public."
  type        = bool
  default     = false
}

variable "alb_security_group_id" {
  description = "Security group for the load balancer."
  type        = string
}

variable "task_security_group_id" {
  description = "Security group for ECS tasks."
  type        = string
}

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate for the HTTPS listener.

    Set it and the ALB serves HTTPS on 443 with a TLS 1.2-minimum policy, and
    port 80 becomes a permanent redirect to it. Firebase ID tokens and the
    alert ingest token cross this boundary on every request, so this is the
    only configuration that is fit to serve real traffic.

    When empty there is no HTTPS listener, and there is a plaintext one only
    if allow_insecure_http is also true. Issuing the certificate requires DNS
    ownership of the API domain, which is why this cannot simply be defaulted.
    The production root refuses an empty value.
  EOT
  type        = string
  default     = ""
}

variable "allow_insecure_http" {
  description = <<-EOT
    Serve the API over plaintext HTTP on port 80 when no certificate is
    configured.

    Default false, and false is the only value fit for anything holding real
    credentials: a public ALB on plain HTTP puts every Firebase ID token and
    the alert ingest token on the wire in clear text, readable by anything on
    the path. Setting this true is an explicit, recorded decision to run a
    throwaway environment without transport security - never production.

    With no certificate and this left false the ALB has no listener at all,
    which is the fail-closed outcome; the aws_lb precondition says so at plan
    time rather than leaving an endpoint that silently answers nothing.
  EOT
  type        = bool
  default     = false
}

variable "alb_logs_bucket" {
  description = "S3 bucket for ALB access logs. Empty disables access logging."
  type        = string
  default     = ""
}

variable "alb_idle_timeout" {
  description = <<-EOT
    ALB idle timeout in seconds.

    Longer than the 60s default on purpose: the incident stream is a
    Server-Sent Events connection that is legitimately quiet between events,
    and a 60s timeout would drop an operator's live view of an investigation
    every minute.
  EOT
  type        = number
  default     = 300
}

variable "enable_deletion_protection" {
  description = "Prevent the load balancer from being deleted."
  type        = bool
  default     = false
}

# --------------------------------------------------------------- images ---
variable "backend_image" {
  description = "Backend image without a tag, e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/aegis/backend."
  type        = string
}

variable "image_tag" {
  description = <<-EOT
    Tag to deploy. The CI pipeline passes the commit SHA.

    "bootstrap" is the placeholder for the very first apply into an empty
    account, before any image has been pushed. The services will not become
    healthy on that tag, which is expected: the first real deploy fixes it.
  EOT
  type        = string
  default     = "bootstrap"
}

variable "cpu_architecture" {
  description = <<-EOT
    X86_64 or ARM64.

    ARM64 Fargate is roughly 20% cheaper per vCPU-hour. Switching requires
    building arm64 images in docker.yml as well; changing only this value
    produces tasks that fail to start with an exec format error.
  EOT
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

# ----------------------------------------------------------------- sizing ---
variable "api_cpu" {
  description = "Fargate CPU units for an API task. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory in MiB for an API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = <<-EOT
    API tasks. One is enough for staging; production should run at least two so
    a task replacement is not an outage.

    Above one, ElastiCache becomes mandatory: SSE events published by one task
    do not reach clients connected to another without the Redis pub/sub bus.
    The environment roots assert this pairing.
  EOT
  type        = number
  default     = 1
}

variable "api_max_count" {
  description = "Autoscaling ceiling for the API service."
  type        = number
  default     = 4
}

variable "api_cpu_target" {
  description = "Target average CPU percentage for API autoscaling."
  type        = number
  default     = 65
}

variable "worker_cpu" {
  description = "Fargate CPU units for a worker task. Investigations are IO bound on LLM calls, not CPU bound."
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Fargate memory in MiB for a worker task."
  type        = number
  default     = 2048
}

variable "worker_min_count" {
  description = <<-EOT
    Minimum worker tasks.

    Zero means the environment costs nothing in worker compute when no incident
    is queued, at the price of a cold start on the first alert: the queue-depth
    alarm needs one 60s period, then a Fargate task takes 30-60s to pull and
    start. Roughly 90-120 seconds before the first investigation begins.

    Production defaults to 1 because that latency lands on a real incident.
  EOT
  type        = number
  default     = 0
}

variable "worker_desired_count" {
  description = "Worker tasks at creation time. Autoscaling owns it afterwards; Terraform ignores later drift."
  type        = number
  default     = 0
}

variable "worker_max_count" {
  description = "Autoscaling ceiling for workers. A hard cap on how much an incident storm can cost."
  type        = number
  default     = 6
}

variable "worker_scaling_mode" {
  description = <<-EOT
    How worker capacity is decided.

      "sqs_backlog" - step scaling on ApproximateNumberOfMessagesVisible. The
                      designed behaviour, and the only mode that can scale to
                      zero. Requires the backend to publish investigation jobs
                      to SQS; see docs/aws-architecture.md, "Known gaps".
      "cpu"         - target tracking on CPU. Works with the current
                      Postgres-backed job queue, but cannot scale to zero
                      because a worker polling an empty queue still burns CPU
                      on its own poll loop.
      "fixed"       - no autoscaling.
  EOT
  type        = string
  default     = "sqs_backlog"

  validation {
    condition     = contains(["sqs_backlog", "cpu", "fixed"], var.worker_scaling_mode)
    error_message = "worker_scaling_mode must be one of: sqs_backlog, cpu, fixed."
  }
}

variable "worker_use_spot" {
  description = <<-EOT
    Run workers on FARGATE_SPOT, which is roughly 70% cheaper.

    Safe here in a way it usually is not: an investigation job lives in
    Postgres with FOR UPDATE SKIP LOCKED, so a two-minute Spot interruption
    notice releases the lease and another worker picks the job up. The
    interruption costs latency, never work.

    The API never runs on Spot: interrupting a request-serving task to save a
    few dollars is a bad trade.
  EOT
  type        = bool
  default     = true
}

variable "queue_name" {
  description = "Investigation queue name, for the scaling alarms."
  type        = string
  default     = ""
}

# ------------------------------------------------------ roles and logging ---
variable "execution_role_arn" {
  description = "ECS task execution role."
  type        = string
}

variable "api_task_role_arn" {
  description = "Task role for the API."
  type        = string
}

variable "worker_task_role_arn" {
  description = "Task role for the worker."
  type        = string
}

variable "migrate_task_role_arn" {
  description = "Task role for the migration task."
  type        = string
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch log retention in days. Never zero: zero means "keep forever",
    which is an unbounded and silently growing bill.
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.log_retention_days > 0
    error_message = "log_retention_days must be greater than zero; infinite retention is not allowed."
  }
}

variable "container_insights" {
  description = <<-EOT
    ECS Container Insights: "disabled", "enabled" or "enhanced".

    Disabled by default. Aegis ships its own OpenTelemetry metrics, so
    Container Insights would be a second, billed copy of data the platform
    already collects. Enable it if you want per-task CPU and memory in
    CloudWatch without running the collector.
  EOT
  type        = string
  default     = "disabled"
}

# ----------------------------------------------------------- application ---
variable "environment_variables" {
  description = "Non-secret environment variables for the API and worker containers."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = <<-EOT
    Environment variable name to Secrets Manager ARN.

    For a JSON secret, append the key with the ECS syntax, e.g.
    "arn:...:secret:db-abc123:password::". ECS resolves these before the
    container starts, so the value never appears in a task definition, in
    Terraform state, or in a console page.
  EOT
  type        = map(string)
  default     = {}
}

variable "firebase_secret_arn" {
  description = <<-EOT
    Secrets Manager ARN holding the Firebase service account JSON.

    The backend loads Firebase credentials from a FILE PATH
    (FIREBASE_SERVICE_ACCOUNT_PATH), and Fargate has no native mechanism for
    projecting a secret as a file. When this is set, the container command is
    wrapped in a shell that writes the injected value to a file inside the
    task's own writable layer before exec'ing the application. The value is
    never echoed. See docs/security.md, "The Firebase credential file".

    Leave empty and the API starts with Firebase unconfigured: it stays up and
    refuses every authenticated request, which is the fail-closed behaviour the
    application already implements.
  EOT
  type        = string
  default     = ""
}

variable "firebase_credential_path" {
  description = "Path the wrapper writes the Firebase service account to inside the container."
  type        = string
  default     = "/tmp/firebase-service-account.json"
}

variable "health_check_path" {
  description = "ALB health check path. /health/ready checks Postgres; /health/live would mark a database-less task healthy."
  type        = string
  default     = "/health/ready"
}

variable "worker_concurrency" {
  description = "In-process investigation concurrency per worker task."
  type        = number
  default     = 2
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}

variable "queue_url" {
  description = "Investigation queue URL, published to the tasks as AEGIS_QUEUE_URL."
  type        = string
  default     = ""
}
