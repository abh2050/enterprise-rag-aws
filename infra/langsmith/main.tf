# OPTIONAL, SEPARATE self-hosted LangSmith on AWS (EKS) using LangChain's official Terraform module.
# NOT part of the core RAG stack; separate state; NOT APPLIED (requires an Enterprise license key and
# incurs EKS + RDS + ElastiCache + S3 cost). See README.md for prerequisites.
#
# Upstream: github.com/langchain-ai/terraform → modules/aws/infra (pinned tag below, verified 2026-09-30).
# The upstream module configures its own aws/helm/kubernetes providers.

terraform {
  required_version = ">= 1.11.0"
  backend "s3" {}
}

variable "name_prefix" {
  type        = string
  description = "1-11 lowercase chars"
  default     = "erpls"
}
variable "environment" {
  type    = string
  default = "dev"
}
variable "region" {
  type    = string
  default = "us-east-2" # org SCP permits regional services only in us-east-2
}

module "langsmith_infra" {
  source = "git::https://github.com/langchain-ai/terraform.git//modules/aws/infra?ref=v0.16.97"

  name_prefix = var.name_prefix
  environment = var.environment
  region      = var.region
  owner       = "platform-team"
  cost_center = "engineering"

  create_vpc                = true  # isolated from the RAG VPC; peer/PrivateLink only if needed
  enable_public_eks_cluster = false # private API endpoint (access via VPN/SSM)
  postgres_source           = "external"
  redis_source              = "external"
}
