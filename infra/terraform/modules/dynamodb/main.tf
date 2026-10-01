# Authoritative state tables (mirror erp_rag.stores.dynamo.TABLES). PITR + KMS + TTL on expiring items.

locals {
  tables = {
    documents       = { hash = "tenant_id", range = "document_id" }
    chunk_manifest  = { hash = "document_id", range = "document_version" }
    workflow        = { hash = "run_id", range = "step" }
    conversations   = { hash = "conversation_key", range = "turn" }
    answer_cache    = { hash = "cache_key", range = null }
    feedback        = { hash = "tenant_user", range = "feedback_id" }
    usage           = { hash = "trace_id", range = "record_id" }
    events          = { hash = "event_id", range = null }
    connector_state = { hash = "connector_id", range = null }
  }
}

resource "aws_dynamodb_table" "this" {
  for_each                    = local.tables
  name                        = "${var.prefix}${each.key}"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = each.value.hash
  range_key                   = each.value.range
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = each.value.hash
    type = "S"
  }
  dynamic "attribute" {
    for_each = each.value.range == null ? [] : [each.value.range]
    content {
      name = attribute.value
      type = "S"
    }
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
