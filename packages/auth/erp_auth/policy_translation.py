"""Explicit translation of source permissions + governance metadata into enforceable records.

Source permissions (who the source system grants) and governance metadata (classification labels from
Purview or manual assignment) are separate inputs and stay separate fields. Neither implies the other:
a Purview label never grants access, and a source grant never lowers a label.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from erp_auth.models import DocPermissionRecord, DocumentStatus, group_principal, label_rank, user_principal


class SourcePermissions(BaseModel):
    """Effective permissions as reported by the source system."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    allowed_users: list[str] = []
    allowed_groups: list[str] = []
    denied_users: list[str] = []
    denied_groups: list[str] = []
    project_ids: list[str] = []
    # Grants the connector could not translate (e.g. anonymous/organization sharing links,
    # SharePoint site groups without directory ids). Any entry makes the ACL incomplete → deny.
    unsupported_grants: list[str] = []
    source_acl_revision: str
    retrieved_at: datetime
    owner: str | None = None


class GovernanceMetadata(BaseModel):
    """Classification metadata. ``source='manual'`` must never be presented as Purview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["manual", "purview", "none"]
    raw_label_id: str | None = None
    mapped_label: str | None = None
    governance_version: str = "none"
    retrieved_at: datetime | None = None
    protected_content: bool = False  # encrypted / IRM-protected; never decrypted
    mapping_version: str = Field(default="governance-map-v1")


class TranslationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    missing_label: Literal["deny"] | str = "deny"  # "deny" or a default label name


def translate(
    *,
    document_id: str,
    permissions: SourcePermissions,
    governance: GovernanceMetadata,
    status: DocumentStatus,
    current_version: str | None,
    acl_version: int,
    title: str,
    source_uri: str,
    source_modified_at: datetime | None,
    legal_hold: bool = False,
    policy: TranslationPolicy | None = None,
) -> DocPermissionRecord:
    policy = policy or TranslationPolicy()
    allowed = {user_principal(u) for u in permissions.allowed_users} | {
        group_principal(g) for g in permissions.allowed_groups
    }
    denied = {user_principal(u) for u in permissions.denied_users} | {
        group_principal(g) for g in permissions.denied_groups
    }

    label = governance.mapped_label
    if label is None and policy.missing_label != "deny":
        label = policy.missing_label
    # Unknown or missing labels are stored verbatim/as sentinel so the policy engine denies them.
    sensitivity = label if label is not None and label_rank(label) is not None else (label or "unlabeled")

    complete = bool(allowed) and not permissions.unsupported_grants
    return DocPermissionRecord(
        tenant_id=permissions.tenant_id,
        document_id=document_id,
        current_version=current_version,
        status=status,
        allowed_principals=frozenset(allowed),
        denied_principals=frozenset(denied),
        project_ids=frozenset(permissions.project_ids),
        sensitivity_label=sensitivity,
        acl_version=acl_version,
        acl_synced_at=permissions.retrieved_at,
        acl_complete=complete,
        governance_version=governance.governance_version,
        governance_source=governance.source,
        revoked=False,
        legal_hold=legal_hold,
        title=title,
        source_uri=source_uri,
        source_modified_at=source_modified_at,
    )
