output "table_arns" { value = [for t in aws_dynamodb_table.this : t.arn] }
output "table_names" { value = { for k, t in aws_dynamodb_table.this : k => t.name } }
