terraform {
  # 1.9 is the floor because the variable validation in variables.tf refers to
  # other variables, which earlier versions reject at parse time.
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Partial backend configuration. Bucket, key, region and lock table are
  # supplied by -backend-config at init time from the bootstrap outputs.
  # Hardcoding them would put an account-specific name into a file every fork
  # of this repository inherits.
  #
  #   terraform init \
  #     -backend-config="bucket=<state bucket>" \
  #     -backend-config="key=production/terraform.tfstate" \
  #     -backend-config="region=<region>" \
  #     -backend-config="dynamodb_table=<lock table>" \
  #     -backend-config="encrypt=true"
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  # Every taggable resource gets these, so cost allocation and the AWS Budgets
  # filter work without per-resource effort.
  default_tags {
    tags = {
      project      = "aegis"
      environment  = "production"
      owner        = var.owner
      "managed-by" = "terraform"
    }
  }
}
