variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "cidr_block" {
  type    = string
  default = "10.40.0.0/16"
}
variable "az_count" {
  type    = number
  default = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "Use 2 or 3 availability zones."
  }
}
variable "single_nat_gateway" {
  type    = bool
  default = true
}
variable "enable_interface_endpoints" {
  type    = bool
  default = true
}
variable "enable_network_firewall" {
  type    = bool
  default = false
}
variable "egress_allowed_domains" {
  description = "Domains reachable from private subnets when the firewall is enabled (TLS SNI / HTTP Host)."
  type        = list(string)
  default = [
    "login.microsoftonline.com",
    "graph.microsoft.com",
    ".purview.azure.com",
    ".purview.azure.net",
  ]
}
variable "deletion_protection" {
  type    = bool
  default = false
}
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "logs_kms_key_arn" { type = string }
