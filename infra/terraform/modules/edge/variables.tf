variable "name" { type = string }
variable "account_id" { type = string }
variable "vpc_id" { type = string }
variable "vpc_cidr" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "api_port" {
  type    = number
  default = 8000
}
variable "alb_certificate_arn" {
  description = "ACM cert for the internal ALB. Empty → HTTP listener reachable only via CloudFront VPC origin (dev only)."
  type        = string
  default     = ""
}
variable "cloudfront_certificate_arn" {
  description = "ACM cert in us-east-1 for custom domains. Empty → *.cloudfront.net default certificate."
  type        = string
  default     = ""
}
variable "domain_names" {
  type    = list(string)
  default = []
}
variable "api_rate_limit_per_5min" {
  type    = number
  default = 1000
}
variable "access_logs_bucket" {
  type    = string
  default = ""
}
variable "deletion_protection" {
  type    = bool
  default = false
}
variable "force_destroy" {
  type    = bool
  default = false
}
