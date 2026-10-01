# S3 buckets:
#  * source    — documents uploaded by the S3 connector path (EventBridge notifications on)
#  * artifacts — landing / quarantine / parsed artifacts (Object Lock for legal holds)
#  * config    — versioned retrieval/model/policy configuration
#  * logs      — S3 server access logs
# All: Block Public Access, SSE-KMS, versioning, TLS-only bucket policy.

locals {
  buckets = {
    source    = "${var.name}-source-${var.account_id}"
    artifacts = "${var.name}-artifacts-${var.account_id}"
    config    = "${var.name}-config-${var.account_id}"
  }
}

resource "aws_s3_bucket" "logs" {
  bucket        = "${var.name}-s3logs-${var.account_id}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # S3 server access logging does not support SSE-KMS destinations
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = var.access_log_retention_days
    }
  }
}

resource "aws_s3_bucket" "this" {
  for_each            = local.buckets
  bucket              = each.value
  force_destroy       = var.force_destroy
  object_lock_enabled = each.key == "artifacts"
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "this" {
  for_each      = aws_s3_bucket.this
  bucket        = each.value.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "${each.key}/"
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    id     = "noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Legal holds are applied per object (ObjectLockLegalHoldStatus=ON); no default retention so
# deletion of non-held artifacts remains possible.
resource "aws_s3_bucket_object_lock_configuration" "artifacts" {
  bucket     = aws_s3_bucket.this["artifacts"].id
  depends_on = [aws_s3_bucket_versioning.this]
}

data "aws_iam_policy_document" "tls_only" {
  for_each = aws_s3_bucket.this
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value.arn, "${each.value.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyUnencryptedUploads"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${each.value.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  for_each   = aws_s3_bucket.this
  bucket     = each.value.id
  policy     = data.aws_iam_policy_document.tls_only[each.key].json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_notification" "source" {
  bucket      = aws_s3_bucket.this["source"].id
  eventbridge = true
}

# ---------------------------------------------------------------- GuardDuty Malware Protection for S3

data "aws_iam_policy_document" "guardduty_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["malware-protection-plan.guardduty.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "guardduty" {
  count              = var.enable_malware_protection ? 1 : 0
  name_prefix        = "${var.name}-gd-mp-"
  assume_role_policy = data.aws_iam_policy_document.guardduty_assume.json
}

data "aws_iam_policy_document" "guardduty" {
  statement {
    sid       = "EventBridgeManagedRule"
    actions   = ["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets", "events:DescribeRule"]
    resources = ["arn:aws:events:${var.region}:${var.account_id}:rule/DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*"]
  }
  statement {
    sid       = "ScanAndTag"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObjectTagging", "s3:GetObjectTagging", "s3:PutObjectVersionTagging", "s3:GetObjectVersionTagging"]
    resources = ["${aws_s3_bucket.this["artifacts"].arn}/landing/*"]
  }
  statement {
    sid       = "BucketConfig"
    actions   = ["s3:GetBucketNotification", "s3:PutBucketNotification", "s3:ListBucket"]
    resources = [aws_s3_bucket.this["artifacts"].arn]
  }
  statement {
    sid       = "ValidateOwnership"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.this["artifacts"].arn}/malware-protection-resource-validation-object"]
  }
  statement {
    sid       = "DecryptKms"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "guardduty" {
  count  = var.enable_malware_protection ? 1 : 0
  role   = aws_iam_role.guardduty[0].id
  policy = data.aws_iam_policy_document.guardduty.json
}

resource "aws_guardduty_malware_protection_plan" "artifacts" {
  count = var.enable_malware_protection ? 1 : 0
  role  = aws_iam_role.guardduty[0].arn
  protected_resource {
    s3_bucket {
      bucket_name     = aws_s3_bucket.this["artifacts"].id
      object_prefixes = ["landing/"]
    }
  }
  actions {
    tagging {
      status = "ENABLED" # writes GuardDutyMalwareScanStatus, read by GuardDutyS3TagScanner
    }
  }
}
