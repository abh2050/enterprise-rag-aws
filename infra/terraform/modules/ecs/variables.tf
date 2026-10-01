variable "name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "image" { type = string }
variable "vpc_id" { type = string }
variable "app_subnet_ids" { type = list(string) }
variable "alb_security_group_id" { type = string }
variable "target_group_arn" { type = string }
variable "api_port" {
  type    = number
  default = 8000
}
variable "api_cpu" {
  type    = number
  default = 512
}
variable "api_memory" {
  type    = number
  default = 1024
}
variable "worker_cpu" {
  type    = number
  default = 1024
}
variable "worker_memory" {
  type    = number
  default = 4096 # app + ClamAV sidecar (clamd keeps signatures in memory, ~1.5-3 GB)
}
variable "clamav_image" {
  description = "ClamAV image (official, amd64). Mirror to ECR and pin by digest for production."
  type        = string
  default     = "clamav/clamav:1.4.6" # verified locally 2026-10-01 (EICAR detected); mirror to ECR + pin digest for prod
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
variable "dynamodb_table_arns" { type = list(string) }
variable "table_prefix" { type = string }
variable "opensearch_domain_arn" { type = string }
variable "opensearch_endpoint" { type = string }
variable "bedrock_resource_arns" { type = list(string) }
variable "data_kms_key_arn" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "audit_kms_key_arn" { type = string }
variable "audit_log_group_arn" { type = string }
variable "audit_log_group_name" { type = string }
variable "secret_arns" {
  description = "Container env var name → Secrets Manager ARN"
  type        = map(string)
  default     = {}
}
variable "artifacts_bucket" { type = string }
variable "artifacts_bucket_arn" { type = string }
variable "source_bucket" { type = string }
variable "source_bucket_arn" { type = string }
variable "worker_queue_arn" { type = string }
variable "worker_queue_url" { type = string }
variable "state_machine_arn" { type = string }
variable "cors_origins" { type = list(string) }
variable "tracing" {
  type    = string
  default = "noop"
}
variable "extra_environment" {
  type    = map(string)
  default = {}
}
variable "log_retention_days" {
  type    = number
  default = 90
}
variable "runtime_secret_arns" {
  description = "Secrets the application reads at runtime by ARN (task role permission)."
  type        = list(string)
  default     = []
}
