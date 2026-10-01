variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "kms_key_arn" { type = string }
variable "force_destroy" {
  type    = bool
  default = false
}
variable "noncurrent_version_days" {
  type    = number
  default = 90
}
variable "access_log_retention_days" {
  type    = number
  default = 365
}
variable "enable_malware_protection" {
  type    = bool
  default = true
}
