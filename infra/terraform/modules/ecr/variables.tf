variable "name" { type = string }
variable "repositories" {
  type    = list(string)
  default = ["app"]
}
variable "kms_key_arn" { type = string }
variable "force_delete" {
  type    = bool
  default = false
}
