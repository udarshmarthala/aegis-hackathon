terraform {
  # 1.9 is the floor for the same reason as the other roots: variable
  # validation blocks here refer to other variables.
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 5.82 is the first release with aws_cloudfront_vpc_origin, which is what
      # lets the load balancer stay internal. Held below 6.0 so the shared
      # modules under ../../modules keep resolving against the same major.
      version = ">= 5.82.0, < 6.0.0"
    }
  }

  # No backend block: LOCAL state, deliberately.
  #
  # This environment is a short-lived hackathon deployment in an account that
  # has no state bucket yet, and CI never runs Terraform against it (the deploy
  # workflow only builds, migrates and rolls services). Creating a state bucket
  # and lock table to hold one operator's state for a few weeks would be
  # infrastructure for its own sake.
  #
  # The cost of that choice is that terraform.tfstate lives on one laptop and
  # is gitignored. Losing it means `terraform import` for each resource, or
  # tearing the environment down by tag. If the deployment outlives the
  # hackathon, run infra/terraform/bootstrap and add:
  #
  #   backend "s3" {}
  #
  # then `terraform init -migrate-state -backend-config=...`.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project      = "aegis"
      environment  = "hackathon"
      owner        = var.owner
      "managed-by" = "terraform"
    }
  }
}
