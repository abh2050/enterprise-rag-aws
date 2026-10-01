variable "name" { type = string }
variable "account_id" { type = string }
variable "source_bucket" { type = string }
variable "kms_key_arn" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "reconcile_schedule" {
  type    = string
  default = "rate(6 hours)"
}
