output "audit_log_group_arn" { value = aws_cloudwatch_log_group.audit.arn }
output "audit_log_group_name" { value = aws_cloudwatch_log_group.audit.name }
output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
