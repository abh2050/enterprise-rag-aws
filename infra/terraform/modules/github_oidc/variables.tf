variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "create_provider" {
  type    = bool
  default = true
}
variable "github_repository" {
  description = "owner/repo"
  type        = string
}
variable "github_environment" { type = string }
variable "ecr_repository_arns" { type = list(string) }
variable "task_role_arns" { type = list(string) }
variable "web_bucket_arn" { type = string }
variable "cloudfront_distribution_arn" { type = string }
