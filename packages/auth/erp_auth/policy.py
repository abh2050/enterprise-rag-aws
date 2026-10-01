"""Deny-by-default document authorization.

``decide`` is the single authoritative check, evaluated against DynamoDB records. ``search_filter``
produces the equivalent pre-filter for OpenSearch; the index copy is never trusted on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from erp_auth.models import (
    AuthzContext,
    Decision,
    DenyReason,
    DocPermissionRecord,
    PolicySettings,
    label_rank,
    labels_at_or_below,
)


def decide(
    ctx: AuthzContext,
    record: DocPermissionRecord | None,
    *,
    settings: PolicySettings,
    version: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Authorize ``ctx`` to read ``record`` (optionally a specific document ``version``)."""
    if record is None:
        return Decision.deny(DenyReason.RECORD_MISSING)
    if record.tenant_id != ctx.tenant_id:
        return Decision.deny(DenyReason.TENANT_MISMATCH)
    if record.revoked:
        return Decision.deny(DenyReason.REVOKED)
    if record.status != "published" or record.current_version is None:
        return Decision.deny(DenyReason.NOT_PUBLISHED)
    if version is not None and version != record.current_version:
        return Decision.deny(DenyReason.VERSION_NOT_CURRENT)
    if not record.acl_complete:
        return Decision.deny(DenyReason.ACL_INCOMPLETE)
    now = now or datetime.now(UTC)
    if record.acl_synced_at is None or now - record.acl_synced_at > timedelta(
        seconds=settings.max_acl_age_seconds
    ):
        return Decision.deny(DenyReason.ACL_STALE)
    if ctx.principals & record.denied_principals:
        return Decision.deny(DenyReason.EXPLICIT_DENY)
    if not (ctx.principals & record.allowed_principals):
        return Decision.deny(DenyReason.NO_GRANT)
    if record.project_ids and not (ctx.projects & record.project_ids):
        return Decision.deny(DenyReason.PROJECT_RESTRICTED)
    rank = label_rank(record.sensitivity_label)
    if rank is None:
        return Decision.deny(DenyReason.LABEL_UNKNOWN)
    clearance_rank = label_rank(ctx.clearance)
    if clearance_rank is None or rank > clearance_rank:
        return Decision.deny(DenyReason.LABEL_ABOVE_CLEARANCE)
    return Decision.allow()


def search_filter(ctx: AuthzContext) -> dict[str, Any]:
    """Mandatory OpenSearch filter for both BM25 and k-NN legs.

    Mirrors ``decide`` for the fields the index carries. Freshness, revocation and current-version
    checks are enforced by the authoritative recheck after retrieval.
    """
    principals = sorted(ctx.principals)
    return {
        "bool": {
            "filter": [
                {"term": {"tenant_id": ctx.tenant_id}},
                {"term": {"publication_status": "published"}},
                {"terms": {"allowed_principals": principals}},
                {"terms": {"sensitivity_label": labels_at_or_below(ctx.clearance)}},
                {
                    "bool": {
                        "should": [
                            {"term": {"project_restricted": False}},
                            {"terms": {"project_ids": sorted(ctx.projects) or ["__none__"]}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            ],
            "must_not": [{"terms": {"denied_principals": principals}}],
        }
    }
