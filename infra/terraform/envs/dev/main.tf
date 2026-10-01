# DEV — minimal footprint (single-AZ OpenSearch node, 1 NAT, 1 api + 1 worker task, no Network Firewall).
variable "region" {
  type    = string
  default = "us-east-2" # only region permitted by the AWS Organization SCP for this account
}
variable "environment" {
  type    = string
  default = "dev"
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
  environment             = var.environment
  region                  = var.region
  vpc_cidr                = "10.40.0.0/16"
  az_count                = 2
  single_nat_gateway      = true
  enable_network_firewall = false
  # Dev cost control: AWS API traffic leaves via NAT over TLS instead of interface endpoints.
  enable_interface_endpoints = false
  deletion_protection        = false
  image_tag                  = var.image_tag
  api_desired_count          = 1
  api_max_count              = 2
  worker_desired_count       = 1
  opensearch_instance_type   = "m7g.medium.search"
  opensearch_instance_count  = 1
  opensearch_volume_gb       = 20
  entra_api_client_id        = var.entra_api_client_id
  entra_allowed_tenants      = var.entra_allowed_tenants
  graph_client_id            = var.graph_client_id
  alarm_emails               = var.alarm_emails
  github_repository          = var.github_repository
}

output "stack" { value = module.stack }
