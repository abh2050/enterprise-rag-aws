# Secret CONTAINERS only. Values are set out of band (console/CLI by an operator) — never in Terraform
# state or source control.
resource "aws_secretsmanager_secret" "this" {
  for_each                = toset(var.secret_names)
  name                    = "${var.name}/${each.key}"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = var.recovery_window_days
}
