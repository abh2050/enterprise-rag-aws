output "cluster_name" { value = aws_ecs_cluster.this.name }
output "tasks_security_group_id" { value = aws_security_group.tasks.id }
output "api_role_arn" { value = aws_iam_role.api.arn }
output "worker_role_arn" { value = aws_iam_role.worker.arn }
output "execution_role_arn" { value = aws_iam_role.execution.arn }
output "task_role_arns" { value = [aws_iam_role.api.arn, aws_iam_role.worker.arn] }
output "service_names" { value = [aws_ecs_service.api.name, aws_ecs_service.worker.name] }
