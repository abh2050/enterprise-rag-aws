# Event-driven ingestion:
#   S3 (source bucket) → EventBridge rule → SQS ingest-events (+DLQ)
#   → EventBridge Pipe → Step Functions (Standard) → sqs:sendMessage.waitForTaskToken → worker-tasks queue
#   → Fargate worker runs the checkpointed pipeline → SendTaskSuccess/Failure
#   EventBridge Scheduler → state machine with {"kind":"reconcile"} every N hours.

resource "aws_sqs_queue" "dlq" {
  for_each                  = toset(["ingest-events", "worker-tasks"])
  name                      = "${var.name}-${each.key}-dlq"
  kms_master_key_id         = var.kms_key_arn
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "ingest_events" {
  name                       = "${var.name}-ingest-events"
  kms_master_key_id          = var.kms_key_arn
  visibility_timeout_seconds = 120
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq["ingest-events"].arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "worker_tasks" {
  name                       = "${var.name}-worker-tasks"
  kms_master_key_id          = var.kms_key_arn
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq["worker-tasks"].arn
    maxReceiveCount     = 3
  })
}

resource "aws_cloudwatch_event_rule" "source_changes" {
  name = "${var.name}-source-changes"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created", "Object Deleted"]
    detail        = { bucket = { name = [var.source_bucket] } }
  })
}

resource "aws_cloudwatch_event_target" "to_sqs" {
  rule = aws_cloudwatch_event_rule.source_changes.name
  arn  = aws_sqs_queue.ingest_events.arn
}

data "aws_iam_policy_document" "ingest_queue" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest_events.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.source_changes.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "ingest_events" {
  queue_url = aws_sqs_queue.ingest_events.id
  policy    = data.aws_iam_policy_document.ingest_queue.json
}

# KMS: EventBridge must be able to encrypt messages to the KMS-encrypted queue (grant via key policy is
# preferred; documented in docs/runbooks/deploy.md as a prerequisite check).

# ---------------------------------------------------------------- state machine

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.name}-ingestion"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name_prefix        = "${var.name}-sfn-"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.worker_tasks.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
  statement {
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
      "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sfn" {
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

resource "aws_sfn_state_machine" "ingestion" {
  name     = "${var.name}-ingestion"
  type     = "STANDARD"
  role_arn = aws_iam_role.sfn.arn
  definition = templatefile("${path.module}/ingestion.asl.json", {
    worker_queue_url = aws_sqs_queue.worker_tasks.url
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = false # executions carry IDs only, but keep payloads out of logs anyway
    level                  = "ERROR"
  }
  tracing_configuration {
    enabled = true
  }
}

# ---------------------------------------------------------------- EventBridge Pipe SQS → SFN

data "aws_iam_policy_document" "pipes_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["pipes.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "pipe" {
  name_prefix        = "${var.name}-pipe-"
  assume_role_policy = data.aws_iam_policy_document.pipes_assume.json
}

data "aws_iam_policy_document" "pipe" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.ingest_events.arn]
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.ingestion.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "pipe" {
  role   = aws_iam_role.pipe.id
  policy = data.aws_iam_policy_document.pipe.json
}

resource "aws_pipes_pipe" "ingest" {
  name     = "${var.name}-ingest"
  role_arn = aws_iam_role.pipe.arn
  source   = aws_sqs_queue.ingest_events.arn
  target   = aws_sfn_state_machine.ingestion.arn
  source_parameters {
    sqs_queue_parameters {
      batch_size = 1
    }
  }
  target_parameters {
    step_function_state_machine_parameters {
      invocation_type = "FIRE_AND_FORGET"
    }
  }
  depends_on = [aws_iam_role_policy.pipe]
}

# ---------------------------------------------------------------- periodic reconciliation

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name_prefix        = "${var.name}-sched-"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["states:StartExecution"], Resource = [aws_sfn_state_machine.ingestion.arn] }]
  })
}

resource "aws_scheduler_schedule" "reconcile" {
  name                = "${var.name}-reconcile"
  schedule_expression = var.reconcile_schedule
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_sfn_state_machine.ingestion.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode([{ body = jsonencode({ kind = "reconcile", source = "s3-${var.source_bucket}" }) }])
  }
}
