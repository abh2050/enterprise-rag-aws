variable "name" { type = string }
variable "account_id" { type = string }
variable "domain_name" { type = string }
variable "engine_version" {
  type    = string
  default = "OpenSearch_3.1" # pinned; matches local opensearchproject/opensearch:3.1.0
}
variable "instance_type" {
  type    = string
  default = "m7g.medium.search"
}
variable "instance_count" {
  type    = number
  default = 1
}
variable "dedicated_master_count" {
  type    = number
  default = 0
}
variable "dedicated_master_type" {
  type    = string
  default = "m7g.medium.search"
}
variable "volume_size_gb" {
  type    = number
  default = 20
}
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "client_security_group_ids" {
  description = "Static name → security group id of clients allowed to reach the domain on 443"
  type        = map(string)
}
variable "client_role_arns" { type = list(string) }
variable "master_role_arn" { type = string }
variable "kms_key_arn" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "create_service_linked_role" {
  description = "Create AWSServiceRoleForAmazonOpenSearchService (once per account). Set false if it already exists."
  type        = bool
  default     = true
}
