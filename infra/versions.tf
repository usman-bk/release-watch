terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # Bucket, key and region come from `terraform init -backend-config=backend.hcl`.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
