# PROD — production: multi-AZ OpenSearch with dedicated masters, NAT per AZ, Network Firewall.
variable "region" {
  type    = string
  default = "us-east-2" # only region permitted by the AWS Organization SCP for this account
}
variable "environment" {
  type    = string
  default = "prod"
}
variable "image_tag" {
  type    = string
  default = "bootstrap"
}
variable "entra_api_client_id" {
  type    = string
  default = ""
}
variable "entra_allowed_tenants" {
  type    = list(string)
  default = []
}
variable "graph_client_id" {
  type    = string
  default = ""
}
variable "alarm_emails" {
  type    = list(string)
  default = []
}
variable "github_repository" {
  type    = string
  default = ""
}

module "stack" {
  source = "../../modules/stack"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
  environment                       = var.environment
  region                            = var.region
  vpc_cidr                          = "10.42.0.0/16"
  az_count                          = 3
  single_nat_gateway                = false
  enable_network_firewall           = true
  deletion_protection               = true
  image_tag                         = var.image_tag
  api_desired_count                 = 2
  api_max_count                     = 12
  worker_desired_count              = 2
  opensearch_instance_type          = "r7g.large.search"
  opensearch_dedicated_master_count = 3
  opensearch_instance_count         = 3
  opensearch_volume_gb              = 200
  entra_api_client_id               = var.entra_api_client_id
  entra_allowed_tenants             = var.entra_allowed_tenants
  graph_client_id                   = var.graph_client_id
  alarm_emails                      = var.alarm_emails
  github_repository                 = var.github_repository
}

output "stack" { value = module.stack }
