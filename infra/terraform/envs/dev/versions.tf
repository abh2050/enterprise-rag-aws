terraform {
  required_version = ">= 1.13.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }
  # Partial configuration: `terraform init -backend-config=backend.hcl` (see backend.hcl.example).
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "enterprise-rag-platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

provider "aws" {
  alias  = "us_east_1" # CloudFront-scope WAF must be created in us-east-1
  region = "us-east-1"
  default_tags {
    tags = {
      Project     = "enterprise-rag-platform"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
