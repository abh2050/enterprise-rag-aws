output "data_key_arn" { value = aws_kms_key.data.arn }
output "logs_key_arn" { value = aws_kms_key.logs.arn }
output "audit_key_arn" { value = aws_kms_key.audit.arn }
