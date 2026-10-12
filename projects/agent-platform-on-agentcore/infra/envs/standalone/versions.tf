terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    # AgentCore Gateway has no `aws` resource; awscc covers it (its targets do not).
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.50"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project    = var.project
      Platform   = var.project
      ManagedBy  = "terraform"
      CostCenter = var.cost_center
    }
  }
}

provider "awscc" {
  region = var.region
}
