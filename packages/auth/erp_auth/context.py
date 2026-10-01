"""Build the trusted ``AuthzContext`` from a verified identity.

Inputs are the verified token, the directory adapter (for overage) and the server-side entitlement map.
Request bodies, headers and query parameters are never consulted.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from erp_auth.directory import DirectoryAdapter
from erp_auth.models import (
    LABEL_RANK,
    AuthzContext,
    SensitivityLabel,
    VerifiedIdentity,
    group_principal,
    user_principal,
)


class RoleEntitlement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clearance: SensitivityLabel | None = None
    projects: list[str] = []
    admin: bool = False


class EntitlementMap(BaseModel):
    """Server-side mapping of app roles (and optionally groups) to clearance/projects/admin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    default_clearance: SensitivityLabel = SensitivityLabel.INTERNAL
    roles: dict[str, RoleEntitlement] = {}
    groups: dict[str, RoleEntitlement] = {}

    @classmethod
    def load(cls, path: Path) -> EntitlementMap:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class ContextBuilder:
    def __init__(self, directory: DirectoryAdapter, entitlements: EntitlementMap) -> None:
        self._directory = directory
        self._entitlements = entitlements

    async def build(self, identity: VerifiedIdentity) -> AuthzContext:
        if identity.groups_overage or identity.groups is None:
            # DirectoryUnavailable propagates: the request fails closed (HTTP 503), never widened.
            groups = await self._directory.groups_for(identity.tenant_id, identity.object_id)
        else:
            groups = identity.groups

        clearance = self._entitlements.default_clearance
        projects: set[str] = set()
        admin = False
        grants = [
            self._entitlements.roles[r] for r in sorted(identity.roles) if r in self._entitlements.roles
        ]
        grants += [self._entitlements.groups[g] for g in sorted(groups) if g in self._entitlements.groups]
        for grant in grants:
            if grant.clearance is not None and LABEL_RANK[grant.clearance] > LABEL_RANK[clearance]:
                clearance = grant.clearance
            projects.update(grant.projects)
            admin = admin or grant.admin

        principals = {user_principal(identity.object_id)} | {group_principal(g) for g in groups}
        return AuthzContext(
            tenant_id=identity.tenant_id,
            user_id=identity.object_id,
            principals=frozenset(principals),
            clearance=clearance,
            projects=frozenset(projects),
            is_admin=admin,
        )
