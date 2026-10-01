variable "name" { type = string }
variable "kms_key_arn" { type = string }
variable "secret_names" {
  type    = list(string)
  default = ["graph-client-secret", "langsmith-api-key"]
}
variable "recovery_window_days" {
  type    = number
  default = 7
}
