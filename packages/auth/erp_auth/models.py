"""Identity, authorization context and permission records.

Identity (who the token says you are) is kept separate from authorization context (what the server
decides you may access). Nothing in ``AuthzContext`` is ever taken from client-supplied request data.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SensitivityLabel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


LABEL_RANK: dict[str, int] = {
    SensitivityLabel.PUBLIC: 0,
    SensitivityLabel.INTERNAL: 1,
    SensitivityLabel.CONFIDENTIAL: 2,
    SensitivityLabel.RESTRICTED: 3,
}


def label_rank(label: str) -> int | None:
    """Rank of a known label, or None for unknown labels (which always deny)."""
    return LABEL_RANK.get(label)


def labels_at_or_below(clearance: SensitivityLabel) -> list[str]:
    limit = LABEL_RANK[clearance]
    return sorted((lbl for lbl, rank in LABEL_RANK.items() if rank <= limit), key=LABEL_RANK.__getitem__)


def user_principal(oid: str) -> str:
    return f"user:{oid}"


def group_principal(gid: str) -> str:
    return f"group:{gid}"


class VerifiedIdentity(BaseModel):
    """Claims extracted from a cryptographically verified access token."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["entra", "dev"]
    issuer: str
    tenant_id: str
    object_id: str
    display_name: str | None = None  # display only; never used for authorization
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] | None = None  # None when the token carried an overage marker
    groups_overage: bool = False
    token_id: str | None = None
    expires_at: datetime


class AuthzContext(BaseModel):
    """Server-constructed authorization context used for every access decision."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    principals: frozenset[str]
    clearance: SensitivityLabel
    projects: frozenset[str] = frozenset()
    is_admin: bool = False

    @property
    def principal_hash(self) -> str:
        material = "|".join(
            [
                self.tenant_id,
                self.clearance,
                ",".join(sorted(self.principals)),
                ",".join(sorted(self.projects)),
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]


DocumentStatus = Literal["pending", "staged", "published", "quarantined", "failed", "deleted"]


class DocPermissionRecord(BaseModel):
    """Authoritative permission + lifecycle record for a document (DynamoDB ``documents`` table)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    document_id: str
    current_version: str | None = None
    status: DocumentStatus
    allowed_principals: frozenset[str] = frozenset()
    denied_principals: frozenset[str] = frozenset()
    project_ids: frozenset[str] = frozenset()
    sensitivity_label: str = SensitivityLabel.RESTRICTED  # unknown/missing → most restrictive
    acl_version: int = 0
    acl_synced_at: datetime | None = None
    acl_complete: bool = False  # False when the source ACL was ambiguous or partially unsupported
    governance_version: str = "none"
    governance_source: Literal["manual", "purview", "none"] = "none"
    revoked: bool = False
    legal_hold: bool = False
    title: str = ""
    source_uri: str = ""
    source_modified_at: datetime | None = None


class DenyReason(StrEnum):
    TENANT_MISMATCH = "tenant_mismatch"
    NOT_PUBLISHED = "not_published"
    REVOKED = "revoked"
    VERSION_NOT_CURRENT = "version_not_current"
    ACL_INCOMPLETE = "acl_incomplete"
    ACL_STALE = "acl_stale"
    NO_GRANT = "no_grant"
    EXPLICIT_DENY = "explicit_deny"
    PROJECT_RESTRICTED = "project_restricted"
    LABEL_UNKNOWN = "label_unknown"
    LABEL_ABOVE_CLEARANCE = "label_above_clearance"
    RECORD_MISSING = "record_missing"
    ACL_VERSION_MISMATCH = "acl_version_mismatch"


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: DenyReason | None = None

    @classmethod
    def allow(cls) -> Decision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: DenyReason) -> Decision:
        return cls(allowed=False, reason=reason)


class PolicySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_acl_age_seconds: int = Field(default=24 * 3600, gt=0)
    policy_version: str = "authz-policy-v1"
