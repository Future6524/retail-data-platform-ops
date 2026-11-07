terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  # No backend block on purpose: choose your own (an encrypted, locked remote backend is recommended).
  # Nothing in this configuration puts a secret VALUE into Terraform state.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = var.name_prefix
      Managed = "terraform"
      Data    = "synthetic-demo"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}
