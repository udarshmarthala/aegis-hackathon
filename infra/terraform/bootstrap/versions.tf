terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # No backend block, deliberately.
  #
  # This configuration CREATES the state bucket and lock table, so it cannot
  # store its own state in them. It runs with local state; the resulting
  # terraform.tfstate is gitignored.
  #
  # The state of the bootstrap is not precious. Everything it creates is
  # protected against deletion and can be re-imported with
  # `terraform import` if the local state is ever lost. Losing the bucket
  # itself is the failure that matters, which is why it has versioning,
  # deletion protection via prevent_destroy, and a policy the deploy role is
  # explicitly denied the ability to change.
  #
  # See docs/terraform.md, "Bootstrap ordering".
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = "aegis"
      environment  = "shared"
      owner        = var.owner
      component    = "bootstrap"
      "managed-by" = "terraform"
    }
  }
}
