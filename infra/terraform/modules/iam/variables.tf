variable "name_prefix" {
  description = "Prefix for every role name, e.g. aegis-staging."
  type        = string
}

variable "environment" {
  description = "Environment name: staging or production."
  type        = string
}

# ------------------------------------------------- application resources ---
variable "artifacts_bucket_arn" {
  description = "Artifacts bucket. Task roles get prefix-scoped access to it, never bucket-wide s3:*."
  type        = string
}

variable "queue_arn" {
  description = "Investigation queue ARN."
  type        = string
}

variable "dlq_arn" {
  description = "Dead-letter queue ARN. The worker may read it for diagnostics; only the redrive policy writes to it."
  type        = string
}

variable "secret_arns" {
  description = <<-EOT
    Every Secrets Manager secret the tasks need, by ARN.

    These are granted to the EXECUTION role, not the task role: ECS resolves
    them before the container starts, so the application never holds
    permission to read its own credentials at runtime. A compromised process
    therefore cannot enumerate secrets.
  EOT
  type        = list(string)
  default     = []
}

variable "kms_key_arn" {
  description = "Customer-managed key used for RDS and secret encryption."
  type        = string
}

variable "log_group_arn" {
  description = "CloudWatch log group the tasks write to."
  type        = string
}

variable "observed_cluster_arns" {
  description = <<-EOT
    ECS clusters Aegis is allowed to OBSERVE - the reference workload, not
    itself. Read-only describe permissions are granted on these.

    "Read broadly, write narrowly" (CLAUDE.md invariant 4) is enforced here in
    IAM, not only in application code: even a fully compromised worker holds no
    permission to change a service unless allow_remediation_actions is set.
  EOT
  type        = list(string)
  default     = []
}

variable "allow_remediation_actions" {
  description = <<-EOT
    Grant the worker task role write permissions (UpdateService,
    RegisterTaskDefinition) against observed_cluster_arns.

    Default false, and that is the fail-closed position required by CLAUDE.md
    invariant 5. Autonomy is also off by default in application config; these
    two must BOTH be turned on before Aegis can change anything, and they are
    owned by different people in different systems on purpose.
  EOT
  type        = bool
  default     = false
}

# ------------------------------------------------------------ github ------
variable "github_repository" {
  description = "owner/repo that may assume the deploy roles, e.g. acme/aegis-2.0."
  type        = string
  default     = ""
}

variable "create_oidc_provider" {
  description = <<-EOT
    Create the GitHub OIDC provider.

    The provider is an ACCOUNT-level singleton: two environments in one account
    cannot each create it. Leave this false in the environment roots and let
    infra/terraform/bootstrap own it.
  EOT
  type        = bool
  default     = false
}

variable "oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider. Required when create_oidc_provider is false and github_repository is set."
  type        = string
  default     = ""
}

variable "deploy_role_subjects" {
  description = <<-EOT
    Allowed values of the token's `sub` claim for the DEPLOY role.

    Scope these tightly. "repo:owner/name:*" lets any branch in the repository
    deploy, which means a pull request branch can deploy to production. Prefer
    environment-scoped subjects such as
    "repo:owner/name:environment:aegis-production", which GitHub only issues
    for a job that has already passed that Environment's reviewers.
  EOT
  type        = list(string)
  default     = []
}

variable "plan_role_subjects" {
  description = "Allowed `sub` claim values for the read-only PLAN role. Wider than the deploy role by design: it can read, not change."
  type        = list(string)
  default     = []
}

variable "tf_state_bucket_arn" {
  description = "Terraform state bucket ARN, for the deploy and plan role policies."
  type        = string
  default     = ""
}

variable "tf_lock_table_arn" {
  description = "Terraform lock table ARN."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
