variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "audit_kms_key_arn" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "data_kms_key_arn" { type = string }
variable "audit_retention_days" {
  type    = number
  default = 2557
}
variable "data_event_bucket_arns" { type = list(string) }
variable "dlq_names" { type = map(string) }
variable "state_machine_arn" { type = string }
variable "alb_arn_suffix" { type = string }
variable "opensearch_domain_name" { type = string }
variable "alarm_emails" {
  type    = list(string)
  default = []
}
variable "force_destroy" {
  type    = bool
  default = false
}
