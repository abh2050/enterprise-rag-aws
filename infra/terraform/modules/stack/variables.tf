variable "project" {
  type    = string
  default = "erp"
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}
variable "region" { type = string }
variable "vpc_cidr" { type = string }
variable "az_count" {
  type    = number
  default = 2
}
variable "single_nat_gateway" {
  type    = bool
  default = true
}
variable "enable_interface_endpoints" {
  description = "Interface VPC endpoints (~$7.3/month per endpoint per AZ). Gateway endpoints are always on."
  type        = bool
  default     = true
}
variable "enable_network_firewall" {
  type    = bool
  default = false
}
variable "enable_malware_protection" {
  description = "GuardDuty Malware Protection for S3. Denied by the org SCP on the current account; ClamAV sidecar is used instead."
  type        = bool
  default     = false
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "image_tag" {
  type    = string
  default = "bootstrap"
}
variable "api_desired_count" {
  type    = number
  default = 1
}
variable "api_max_count" {
  type    = number
  default = 4
}
variable "worker_desired_count" {
  type    = number
  default = 1
}
variable "opensearch_instance_type" {
  type    = string
  default = "m7g.medium.search"
}
variable "opensearch_instance_count" {
  type    = number
  default = 1
}
variable "opensearch_dedicated_master_count" {
  type    = number
  default = 0
}
variable "opensearch_volume_gb" {
  type    = number
  default = 20
}
variable "opensearch_master_role_arn" {
  description = "IAM role mapped as OpenSearch FGAC master (break-glass admin). Defaults to the worker task role."
  type        = string
  default     = ""
}
variable "alb_certificate_arn" {
  type    = string
  default = ""
}
variable "cloudfront_certificate_arn" {
  type    = string
  default = ""
}
variable "domain_names" {
  type    = list(string)
  default = []
}
variable "alarm_emails" {
  type    = list(string)
  default = []
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
variable "bedrock_in_region_models" {
  description = "Foundation models invoked in-region (must match config/models.bedrock.yaml)."
  type        = list(string)
  default     = ["amazon.titan-embed-text-v2:0", "amazon.nova-lite-v1:0"]
}
variable "bedrock_inference_profiles" {
  type    = list(string)
  default = ["us.amazon.nova-pro-v1:0"]
}
variable "bedrock_geo_models" {
  type    = list(string)
  default = ["amazon.nova-pro-v1:0"]
}
variable "bedrock_geo_regions" {
  description = "Destination regions of the US geo inference profile from source us-east-2 (Nova Pro model card)."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}
variable "github_repository" {
  type    = string
  default = ""
}
variable "create_github_oidc_provider" {
  type    = bool
  default = true
}
