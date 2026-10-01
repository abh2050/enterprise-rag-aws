output "state_machine_arn" { value = aws_sfn_state_machine.ingestion.arn }
output "worker_queue_arn" { value = aws_sqs_queue.worker_tasks.arn }
output "worker_queue_url" { value = aws_sqs_queue.worker_tasks.url }
output "dlq_names" { value = { for k, q in aws_sqs_queue.dlq : k => q.name } }
output "state_machine_name" { value = aws_sfn_state_machine.ingestion.name }
