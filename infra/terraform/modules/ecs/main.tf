# ECS Fargate: one cluster, two services (api, worker) from the same image, scoped task roles,
# read-only root filesystem, secrets from Secrets Manager, deployment circuit breaker with rollback.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_cloudwatch_log_group" "service" {
  for_each          = toset(["api", "worker"])
  name              = "/${var.name}/ecs/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
}

# ---------------------------------------------------------------- execution role (pull image, logs, secrets)

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  count = length(var.secret_arns) > 0 ? 1 : 0
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = values(var.secret_arns)
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.data_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  count  = length(var.secret_arns) > 0 ? 1 : 0
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets[0].json
}

# ---------------------------------------------------------------- task roles

data "aws_iam_policy_document" "common" {
  statement {
    sid = "DynamoDB"
    actions = [
      "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:PutItem",
      "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable",
    ]
    resources = var.dynamodb_table_arns
  }
  statement {
    sid       = "OpenSearch"
    actions   = ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete", "es:ESHttpHead"]
    resources = ["${var.opensearch_domain_arn}/*"]
  }
  statement {
    sid       = "BedrockApprovedModelsOnly" # Converse is authorized as bedrock:InvokeModel
    actions   = ["bedrock:InvokeModel"]
    resources = var.bedrock_resource_arns
  }
  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.data_kms_key_arn]
  }
  statement {
    sid       = "AuditLogWriteOnly"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${var.audit_log_group_arn}:*"]
  }
  statement {
    sid       = "AuditKms"
    actions   = ["kms:GenerateDataKey", "kms:Encrypt"]
    resources = [var.audit_kms_key_arn]
  }
  statement {
    sid       = "Secrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.runtime_secret_arns
  }
  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "api" {
  name_prefix        = "${var.name}-api-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.common.json]
  statement {
    sid       = "ReadArtifactsForCitedDownloads"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${var.artifacts_bucket_arn}/landing/*"]
  }
  statement {
    sid       = "AdminReplayViaStepFunctions"
    actions   = ["states:StartExecution"]
    resources = [var.state_machine_arn]
  }
  statement {
    sid       = "AdminActionsTombstone"
    actions   = ["s3:DeleteObject", "s3:ListBucket", "s3:GetObjectLegalHold"]
    resources = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role" "worker" {
  name_prefix        = "${var.name}-worker-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker" {
  source_policy_documents = [data.aws_iam_policy_document.common.json]
  statement {
    sid       = "ReadSources"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "s3:HeadObject"]
    resources = [var.source_bucket_arn, "${var.source_bucket_arn}/*"]
  }
  statement {
    sid = "Artifacts"
    actions = [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetObjectTagging",
      "s3:PutObjectLegalHold", "s3:GetObjectLegalHold",
    ]
    resources = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
  }
  statement {
    sid       = "Textract"
    actions   = ["textract:DetectDocumentText"]
    resources = ["*"]
  }
  statement {
    sid       = "TaskQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [var.worker_queue_arn]
  }
  statement {
    sid       = "StepFunctionsCallbacks"
    actions   = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]
    resources = [var.state_machine_arn]
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# ---------------------------------------------------------------- task definitions + services

locals {
  common_env = merge({
    ERP_ENVIRONMENT                 = var.environment
    ERP_AUTH_PROVIDER               = "entra"
    ERP_MODEL_PROVIDER              = "bedrock"
    ERP_AWS_REGION                  = var.region
    ERP_BEDROCK_ALLOWED_REGIONS     = jsonencode([var.region])
    ERP_OPENSEARCH_URL              = var.opensearch_endpoint
    ERP_OPENSEARCH_AUTH             = "sigv4"
    ERP_DYNAMODB_ENDPOINT           = ""
    ERP_TABLE_PREFIX                = var.table_prefix
    ERP_ARTIFACT_STORE              = "s3"
    ERP_ARTIFACT_BUCKET             = var.artifacts_bucket
    ERP_SCANNER                     = "clamav" # GuardDuty Malware Protection is denied by org SCP
    ERP_CLAMD_HOST                  = "127.0.0.1"
    ERP_TEXTRACT_ENABLED            = "true"
    ERP_S3_SOURCES                  = jsonencode([var.source_bucket])
    ERP_CORS_ORIGINS                = jsonencode(var.cors_origins)
    ERP_AUDIT_LOG_GROUP             = var.audit_log_group_name
    ERP_TRACING                     = var.tracing
    ERP_LOCAL_SOURCES               = "{}"
    ERP_WORKER_QUEUE_URL            = var.worker_queue_url
    ERP_INGESTION_STATE_MACHINE_ARN = var.state_machine_arn
  }, var.extra_environment)
  secrets = [for k, arn in var.secret_arns : { name = k, valueFrom = arn }]
  log_config = { for svc in ["api", "worker"] : svc => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.service[svc].name
      awslogs-region        = var.region
      awslogs-stream-prefix = svc
    }
  } }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  volume {
    name = "tmp"
  }
  container_definitions = jsonencode([{
    name                   = "api"
    image                  = var.image
    essential              = true
    command                = ["uvicorn", "erp_api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", tostring(var.api_port), "--proxy-headers", "--no-server-header"]
    readonlyRootFilesystem = true
    user                   = "app"
    portMappings           = [{ containerPort = var.api_port, protocol = "tcp" }]
    environment            = [for k, v in local.common_env : { name = k, value = v }]
    secrets                = local.secrets
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
    logConfiguration       = local.log_config["api"]
  }])
}

# Worker runs on X86_64 because the official ClamAV image is published for linux/amd64 only; the app
# image is built multi-arch (arm64 for the API, amd64 for the worker).
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  volume {
    name = "tmp"
  }
  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = var.image
      essential              = true
      command                = ["python", "-m", "erp_ingestion.sqs_worker"]
      readonlyRootFilesystem = true
      user                   = "app"
      environment = [for k, v in merge(local.common_env, {
        ERP_SERVICE_ROLE            = "worker"
        ERP_OPENSEARCH_API_ROLE_ARN = aws_iam_role.api.arn # worker (FGAC master) maps the API role at startup
      }) : { name = k, value = v }]
      secrets          = local.secrets
      mountPoints      = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
      logConfiguration = local.log_config["worker"]
      dependsOn        = [{ containerName = "clamav", condition = "HEALTHY" }]
    },
    {
      name      = "clamav"
      image     = var.clamav_image
      essential = true
      # clamd listens on 127.0.0.1:3310 inside the task (awsvpc shares the loopback); freshclam updates
      # signatures through NAT. clamd defaults (1.4.6): StreamMaxLength = MaxFileSize = 100 MB ≥
      # ERP_MAX_FILE_BYTES (50 MB); larger uploads get a clamd ERROR → quarantined, never treated as clean.
      environment = [{ name = "CLAMAV_NO_MILTERD", value = "true" }]
      healthCheck = {
        command     = ["CMD-SHELL", "clamdscan --ping 3 || exit 1"]
        interval    = 30
        timeout     = 10
        retries     = 10
        startPeriod = 300
      }
      logConfiguration = local.log_config["worker"]
    },
  ])
}

resource "aws_security_group" "tasks" {
  name_prefix = "${var.name}-tasks-"
  description = "ECS tasks: ingress from internal ALB only; egress HTTPS"
  vpc_id      = var.vpc_id
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = var.alb_security_group_id
  from_port                    = var.api_port
  to_port                      = var.api_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0" # VPC endpoints + OpenSearch (in-VPC) + Microsoft endpoints via NAT/firewall
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_ecs_service" "api" {
  name                   = "api"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = var.api_desired_count
  launch_type            = "FARGATE"
  enable_execute_command = false
  propagate_tags         = "SERVICE"
  network_configuration {
    subnets          = var.app_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "api"
    container_port   = var.api_port
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle { ignore_changes = [desired_count] }
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"
  network_configuration {
    subnets          = var.app_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = min(var.api_desired_count, var.api_max_count)
  max_capacity       = var.api_max_count
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${var.name}-api-cpu"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  policy_type        = "TargetTrackingScaling"
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
