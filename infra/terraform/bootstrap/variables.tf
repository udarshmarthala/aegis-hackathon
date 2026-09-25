variable "aws_region" {
  description = "Region everything lives in. Aegis is deliberately single-region."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Owning team or individual. Appears in the tag on every resource."
  type        = string
  default     = "platform"
}

variable "state_bucket_name" {
  description = <<-EOT
    Globally unique name for the Terraform state bucket.

    Include the account id or another unique token: S3 bucket names are global
    across all AWS customers, and "aegis-tfstate" is long gone.
  EOT
  type        = string
}

variable "lock_table_name" {
  description = "DynamoDB table used for state locking."
  type        = string
  default     = "aegis-terraform-locks"
}

variable "github_repository" {
  description = "owner/repo allowed to assume AWS roles via OIDC, e.g. acme/aegis-2.0. Empty skips the OIDC provider."
  type        = string
  default     = ""
}

variable "create_oidc_provider" {
  description = <<-EOT
    Create the GitHub OIDC provider.

    It is an account-level singleton. If the account already has one (another
    repository, another team), set this false and pass the existing ARN to the
    environment roots instead.
  EOT
  type        = bool
  default     = true
}

variable "ecr_repositories" {
  description = "ECR repositories to create. Shared across environments: an image is built once and the same digest is promoted."
  type        = list(string)
  default     = ["aegis/backend", "aegis/web"]
}

variable "ecr_untagged_expiry_days" {
  description = "Days before an untagged image layer is expired. Untagged images are build residue and are billed like any other storage."
  type        = number
  default     = 7
}

variable "ecr_max_tagged_images" {
  description = "Tagged images kept per repository. Must comfortably exceed the number of revisions you would ever roll back across."
  type        = number
  default     = 50
}
