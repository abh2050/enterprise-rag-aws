# Amazon OpenSearch Service: VPC-only, pinned engine version, encryption at rest (KMS) and in transit,
# HTTPS enforced with TLS 1.2+, fine-grained access control with IAM master role, audit/slow logs.

# VPC domains require this account-wide service-linked role (CreateDomain fails without it).
resource "aws_iam_service_linked_role" "opensearch" {
  count            = var.create_service_linked_role ? 1 : 0
  aws_service_name = "opensearchservice.amazonaws.com"
}

resource "aws_security_group" "this" {
  name_prefix = "${var.name}-os-"
  description = "OpenSearch: HTTPS from application tasks only"
  vpc_id      = var.vpc_id
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "https_from_app" {
  for_each                     = var.client_security_group_ids # static keys → plannable before apply
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.value
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["index-slow", "search-slow", "es-application", "audit"])
  name              = "/${var.name}/opensearch/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
}

data "aws_iam_policy_document" "log_publishing" {
  statement {
    actions   = ["logs:PutLogEvents", "logs:CreateLogStream", "logs:PutLogEventsBatch"]
    resources = [for g in aws_cloudwatch_log_group.this : "${g.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["es.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "this" {
  policy_name     = "${var.name}-opensearch-logs"
  policy_document = data.aws_iam_policy_document.log_publishing.json
}

resource "aws_opensearch_domain" "this" {
  domain_name    = var.domain_name
  engine_version = var.engine_version

  cluster_config {
    instance_type            = var.instance_type
    instance_count           = var.instance_count
    zone_awareness_enabled   = var.instance_count > 1
    dedicated_master_enabled = var.dedicated_master_count > 0
    dedicated_master_count   = var.dedicated_master_count > 0 ? var.dedicated_master_count : null
    dedicated_master_type    = var.dedicated_master_count > 0 ? var.dedicated_master_type : null
    dynamic "zone_awareness_config" {
      for_each = var.instance_count > 1 ? [1] : []
      content {
        availability_zone_count = min(var.instance_count, length(var.subnet_ids))
      }
    }
  }

  vpc_options {
    subnet_ids         = slice(var.subnet_ids, 0, var.instance_count > 1 ? min(var.instance_count, length(var.subnet_ids)) : 1)
    security_group_ids = [aws_security_group.this.id]
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.volume_size_gb
  }

  encrypt_at_rest {
    enabled    = true
    kms_key_id = var.kms_key_arn
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-PFS-2023-10"
  }

  advanced_security_options {
    enabled                        = true
    anonymous_auth_enabled         = false
    internal_user_database_enabled = false
    master_user_options {
      master_user_arn = var.master_role_arn
    }
  }

  snapshot_options {
    automated_snapshot_start_hour = 3
  }

  dynamic "log_publishing_options" {
    for_each = {
      INDEX_SLOW_LOGS     = "index-slow"
      SEARCH_SLOW_LOGS    = "search-slow"
      ES_APPLICATION_LOGS = "es-application"
      AUDIT_LOGS          = "audit"
    }
    content {
      log_type                 = log_publishing_options.key
      cloudwatch_log_group_arn = aws_cloudwatch_log_group.this[log_publishing_options.value].arn
    }
  }

  depends_on = [aws_cloudwatch_log_resource_policy.this, aws_iam_service_linked_role.opensearch]
}

data "aws_iam_policy_document" "access" {
  statement {
    actions   = ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete", "es:ESHttpHead"]
    resources = ["${aws_opensearch_domain.this.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = concat([var.master_role_arn], var.client_role_arns)
    }
  }
}

resource "aws_opensearch_domain_policy" "this" {
  domain_name     = aws_opensearch_domain.this.domain_name
  access_policies = data.aws_iam_policy_document.access.json
}
