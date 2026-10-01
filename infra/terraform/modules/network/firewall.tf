# AWS Network Firewall: explicit, domain-allowlisted egress (Microsoft identity/Graph/Purview + AWS).
# Enabled in staging/prod; dev relies on security groups + NAT to keep cost down (documented).

resource "aws_networkfirewall_rule_group" "egress_allowlist" {
  count    = var.enable_network_firewall ? 1 : 0
  name     = "${var.name}-egress-allowlist"
  type     = "STATEFUL"
  capacity = 100
  rule_group {
    rules_source {
      rules_source_list {
        generated_rules_type = "ALLOWLIST"
        target_types         = ["TLS_SNI", "HTTP_HOST"]
        targets              = var.egress_allowed_domains
      }
    }
  }
}

resource "aws_networkfirewall_firewall_policy" "this" {
  count = var.enable_network_firewall ? 1 : 0
  name  = "${var.name}-egress"
  firewall_policy {
    stateless_default_actions          = ["aws:forward_to_sfe"]
    stateless_fragment_default_actions = ["aws:forward_to_sfe"]
    stateful_rule_group_reference {
      resource_arn = aws_networkfirewall_rule_group.egress_allowlist[0].arn
    }
  }
}

resource "aws_networkfirewall_firewall" "this" {
  count               = var.enable_network_firewall ? 1 : 0
  name                = "${var.name}-egress"
  firewall_policy_arn = aws_networkfirewall_firewall_policy.this[0].arn
  vpc_id              = aws_vpc.this.id
  delete_protection   = var.deletion_protection
  dynamic "subnet_mapping" {
    for_each = aws_subnet.firewall[*].id
    content {
      subnet_id = subnet_mapping.value
    }
  }
}

locals {
  firewall_endpoints = var.enable_network_firewall ? {
    for s in aws_networkfirewall_firewall.this[0].firewall_status[0].sync_states :
    s.availability_zone => s.attachment[0].endpoint_id
  } : {}
}
