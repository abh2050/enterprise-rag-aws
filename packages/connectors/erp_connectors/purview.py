"""Microsoft Purview governance adapter (versioned).

Purview supplies *classification metadata only*. It is not a document store and not a source of
effective permissions; nothing here grants access.

Two verified capability paths (no parity assumed between M365 and S3):

1. **M365 / SharePoint items — sensitivity labels via Graph v1.0**
   ``POST /drives/{drive-id}/items/{item-id}/extractSensitivityLabels`` (app permission ``Files.Read.All``)
   returns ``labels[{sensitivityLabelId, assignmentMethod, tenantId}]``; ``423 Locked`` with
   ``fileDoubleKeyEncrypted`` / ``fileDecryptionNotSupported`` → protected content (quarantined, never
   decrypted); ``fileDecryptionDeferred`` → transient. Label GUIDs are mapped to internal labels through
   the versioned mapping file (the org-wide label listing API is Graph **beta** only, so it is not used).

2. **Amazon S3 — classifications via Purview Data Map** (multicloud scanning connector; us-west-2 buckets
   are scanned in us-west-2). ``GET {endpoint}/datamap/api/atlas/v2/entity/uniqueAttribute/type/{typeName}
   ?attr:qualifiedName=...&api-version=2023-09-01`` (scope ``https://purview.azure.net/.default``) returns
   ``entity.classifications[].typeName``. The Atlas type name and qualifiedName format for S3 objects are
   configuration (**unverified** until checked against a real scan).

Missing/stale metadata → ``mapped_label=None`` → the policy engine denies (unless the collection policy
explicitly configures a default label). Live status: implemented-not-live-tested → blocked (no tenant).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from erp_auth.models import LABEL_RANK, SensitivityLabel
from erp_auth.policy_translation import GovernanceMetadata
from erp_connectors.base import SourceRef

GRAPH = "https://graph.microsoft.com/v1.0"
AppToken = Callable[[str], Awaitable[str]]
PROTECTED_CODES = {"fileDoubleKeyEncrypted", "fileDecryptionNotSupported"}


class GovernanceMapping(BaseModel):
    """Versioned mapping from Purview label GUIDs / classification names to internal labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mapping_version: str
    label_ids: dict[str, SensitivityLabel] = {}
    classifications: dict[str, SensitivityLabel] = {}
    unmapped_classification_label: SensitivityLabel | None = None  # None → unknown classifications ignored
    max_metadata_age_seconds: int = 24 * 3600

    @classmethod
    def load(cls, path: Path) -> GovernanceMapping:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class TransientGovernanceError(Exception):
    pass


class GraphSensitivityLabelAdapter:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: AppToken,
        mapping: GovernanceMapping,
        *,
        tenant_id: str,
        drive_id: str,
    ) -> None:
        self._http, self._token, self._map = http, token, mapping
        self.tenant_id, self.drive_id = tenant_id, drive_id
        self.version = f"purview-graph-labels:{mapping.mapping_version}"

    async def get(self, ref: SourceRef) -> GovernanceMetadata:
        token = await self._token(self.tenant_id)
        resp = await self._http.post(
            f"{GRAPH}/drives/{quote(self.drive_id)}/items/{quote(ref.source_key)}/extractSensitivityLabels",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        now = datetime.now(UTC)
        if resp.status_code == 423:
            code = resp.json().get("error", {}).get("code", "")
            if code in PROTECTED_CODES:
                return GovernanceMetadata(
                    source="purview",
                    protected_content=True,
                    retrieved_at=now,
                    governance_version=f"{self.version}:{code}",
                    mapping_version=self._map.mapping_version,
                )
            raise TransientGovernanceError(code or "locked")
        if resp.status_code != 200:
            raise TransientGovernanceError(f"status {resp.status_code}")
        labels = resp.json().get("value", {}).get("labels", [])
        own = [lbl for lbl in labels if lbl.get("tenantId") in (None, self.tenant_id)]
        mapped = [self._map.label_ids.get(str(lbl.get("sensitivityLabelId"))) for lbl in own]
        raw = ",".join(sorted(str(lbl.get("sensitivityLabelId")) for lbl in own)) or None
        if not own:
            label = None  # unlabeled → policy decides (default deny)
        elif any(m is None for m in mapped):
            label = None  # unknown label GUID → deny rather than guess
        else:
            label = max((m for m in mapped if m is not None), key=lambda x: LABEL_RANK[x]).value
        return GovernanceMetadata(
            source="purview",
            raw_label_id=raw,
            mapped_label=label,
            retrieved_at=now,
            governance_version=f"{self.version}:{raw or 'none'}",
            mapping_version=self._map.mapping_version,
        )


class PurviewDataMapGovernance:
    API_VERSION = "2023-09-01"

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: AppToken,
        mapping: GovernanceMapping,
        *,
        tenant_id: str,
        endpoint: str,
        bucket: str,
        type_name: str,
        qualified_name_template: str = "s3://{bucket}/{key}",
    ):
        self._http, self._token, self._map = http, token, mapping
        self.tenant_id, self.endpoint, self.bucket = tenant_id, endpoint.rstrip("/"), bucket
        self.type_name, self.qn_template = type_name, qualified_name_template
        self.version = f"purview-datamap:{mapping.mapping_version}"

    async def get(self, ref: SourceRef) -> GovernanceMetadata:
        token = await self._token(self.tenant_id)
        qn = self.qn_template.format(bucket=self.bucket, key=ref.source_key)
        resp = await self._http.get(
            f"{self.endpoint}/datamap/api/atlas/v2/entity/uniqueAttribute/type/{quote(self.type_name)}",
            params={
                "attr:qualifiedName": qn,
                "minExtInfo": "true",
                "ignoreRelationships": "true",
                "api-version": self.API_VERSION,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        now = datetime.now(UTC)
        if resp.status_code == 404:
            return GovernanceMetadata(
                source="none",
                retrieved_at=now,
                governance_version=f"{self.version}:not_scanned",
                mapping_version=self._map.mapping_version,
            )
        if resp.status_code != 200:
            raise TransientGovernanceError(f"status {resp.status_code}")
        entity = resp.json().get("entity", {})
        updated_ms = int(entity.get("updateTime") or 0)
        if updated_ms and time.time() - updated_ms / 1000 > self._map.max_metadata_age_seconds:
            return GovernanceMetadata(
                source="purview",
                retrieved_at=now,
                governance_version=f"{self.version}:stale",
                mapping_version=self._map.mapping_version,
            )  # stale → no label → deny
        names = sorted({str(c.get("typeName")) for c in entity.get("classifications", []) or []})
        labels: list[SensitivityLabel] = []
        for name in names:
            if name in self._map.classifications:
                labels.append(self._map.classifications[name])
            elif self._map.unmapped_classification_label is not None:
                labels.append(self._map.unmapped_classification_label)
        label = max(labels, key=lambda x: LABEL_RANK[x]).value if labels else None
        return GovernanceMetadata(
            source="purview",
            raw_label_id=",".join(names) or None,
            mapped_label=label,
            retrieved_at=now,
            governance_version=f"{self.version}:{entity.get('version', 0)}",
            mapping_version=self._map.mapping_version,
        )
