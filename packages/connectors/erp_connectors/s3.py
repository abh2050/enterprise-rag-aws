"""Amazon S3 source connector.

Layout mirrors the local connector: ``s3://<bucket>/<prefix>/<collection>/_access.yaml`` is the source
permission record for objects in that collection (S3 objects carry no end-user ACLs). Object revision is
the S3 ``VersionId`` when versioning is enabled, else the ``ETag``.

Change detection: in AWS, S3 → EventBridge → SQS delivers events to the worker; ``list_changes`` is the
periodic reconciliation path. Live status: implemented-not-live-tested (contract-tested with Stubber).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from erp_auth.policy_translation import GovernanceMetadata, SourcePermissions
from erp_connectors.access_manifest import (
    ACCESS_FILE,
    SUPPORTED_SUFFIXES,
    AccessFile,
    effective_access,
    parse_access,
)
from erp_connectors.base import ChangeEvent, ConnectorError, FetchedContent, SourceRef


class S3Connector:
    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        *,
        prefix: str = "",
        name: str | None = None,
        max_object_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._s3 = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.name = name or f"s3-{bucket}"
        self._max = max_object_bytes
        self._access_cache: dict[str, tuple[str, AccessFile | None]] = {}

    async def _list(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            paginator = self._s3.get_paginator("list_objects_v2")
            objects: list[dict[str, Any]] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
                objects.extend(page.get("Contents", []))
            return objects

        return await asyncio.to_thread(op)

    async def _access(self, collection_prefix: str) -> tuple[str, AccessFile | None]:
        key = f"{collection_prefix}{ACCESS_FILE}"
        try:
            resp = await asyncio.to_thread(self._s3.get_object, Bucket=self.bucket, Key=key)
            body = await asyncio.to_thread(resp["Body"].read)
        except Exception:
            return "none", None
        text = body.decode("utf-8", errors="replace")
        return hashlib.sha256(body).hexdigest()[:16], parse_access(text)

    def _collection_prefix(self, key: str) -> str:
        return key.rsplit("/", 1)[0] + "/" if "/" in key else ""

    async def _documents(self) -> list[tuple[dict[str, Any], str, AccessFile | None, str]]:
        objects = await self._list()
        docs: list[tuple[dict[str, Any], str, AccessFile | None, str]] = []
        cache: dict[str, tuple[str, AccessFile | None]] = {}
        for obj in objects:
            key: str = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if filename.startswith("_") or suffix not in SUPPORTED_SUFFIXES:
                continue
            cp = self._collection_prefix(key)
            if cp not in cache:
                cache[cp] = await self._access(cp)
            acl_hash, access = cache[cp]
            docs.append((obj, cp, access, acl_hash))
        self._access_cache = cache
        return docs

    def _ref(self, key: str, access: AccessFile | None) -> SourceRef:
        return SourceRef(
            source=self.name, source_key=key, tenant_id=access.tenant_id if access else "unassigned"
        )

    async def ref_for_key(self, key: str) -> SourceRef:
        """Resolve tenant ownership for an event key from the collection's access manifest."""
        _, access = await self._access(self._collection_prefix(key))
        return self._ref(key, access)

    async def list_all(self) -> list[SourceRef]:
        return [self._ref(o["Key"], a) for o, _, a, _ in await self._documents()]

    async def list_changes(self, cursor: dict[str, str]) -> tuple[list[ChangeEvent], dict[str, str]]:
        now = datetime.now(UTC)
        events: list[ChangeEvent] = []
        new_cursor: dict[str, str] = {}
        for obj, _cp, access, acl_hash in await self._documents():
            ref = self._ref(obj["Key"], access)
            revision = str(obj.get("ETag", "")).strip('"')
            filename = obj["Key"].rsplit("/", 1)[-1]
            eff = effective_access(access, filename).model_dump_json() if access else "none"
            acl_rev = hashlib.sha256(f"{acl_hash}:{eff}".encode()).hexdigest()[:16]
            new_cursor[ref.source_key] = f"{revision}:{acl_rev}:{ref.tenant_id}"
            prev = cursor.get(ref.source_key, "").split(":")
            if len(prev) < 3 or prev[0] != revision:
                events.append(ChangeEvent(kind="upsert", ref=ref, source_revision=revision, detected_at=now))
            elif prev[1] != acl_rev or prev[2] != ref.tenant_id:
                events.append(
                    ChangeEvent(kind="permissions", ref=ref, source_revision=acl_rev, detected_at=now)
                )
        for key, value in cursor.items():
            if key not in new_cursor:
                parts = value.split(":")
                tenant = parts[2] if len(parts) >= 3 else "unassigned"
                events.append(
                    ChangeEvent(
                        kind="delete",
                        ref=SourceRef(source=self.name, source_key=key, tenant_id=tenant),
                        source_revision="deleted",
                        detected_at=now,
                    )
                )
        return events, new_cursor

    async def fetch(self, ref: SourceRef) -> FetchedContent:
        head = await asyncio.to_thread(self._s3.head_object, Bucket=self.bucket, Key=ref.source_key)
        if int(head.get("ContentLength", 0)) > self._max:
            raise ConnectorError("object exceeds maximum size")
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": ref.source_key}
        if head.get("VersionId"):
            kwargs["VersionId"] = head["VersionId"]  # pin the exact revision we checked
        resp = await asyncio.to_thread(self._s3.get_object, **kwargs)
        data: bytes = await asyncio.to_thread(resp["Body"].read)
        _, access = await self._access(self._collection_prefix(ref.source_key))
        filename = ref.source_key.rsplit("/", 1)[-1]
        eff = effective_access(access, filename) if access else None
        modified = head.get("LastModified")
        return FetchedContent(
            ref=ref,
            filename=filename,
            data=data,
            source_revision=str(head.get("VersionId") or head.get("ETag", "")).strip('"'),
            source_modified_at=modified if isinstance(modified, datetime) else None,
            source_uri=f"s3://{self.bucket}/{ref.source_key}",
            title=eff.title if eff and eff.title else filename.rsplit(".", 1)[0].replace("-", " ").title(),
            owner=eff.owner if eff else None,
            series_id=eff.series_id if eff else None,
            effective_date=(
                datetime(
                    eff.effective_date.year, eff.effective_date.month, eff.effective_date.day, tzinfo=UTC
                )
                if eff and eff.effective_date
                else None
            ),
            legal_hold=eff.legal_hold if eff else False,
        )

    async def get_permissions(self, ref: SourceRef) -> SourcePermissions:
        acl_hash, access = await self._access(self._collection_prefix(ref.source_key))
        now = datetime.now(UTC)
        if access is None:
            return SourcePermissions(
                tenant_id=ref.tenant_id,
                unsupported_grants=["missing_or_invalid_access_file"],
                source_acl_revision="none",
                retrieved_at=now,
            )
        eff = effective_access(access, ref.source_key.rsplit("/", 1)[-1])
        return SourcePermissions(
            tenant_id=access.tenant_id,
            allowed_users=eff.allowed_users,
            allowed_groups=eff.allowed_groups,
            denied_users=eff.denied_users,
            denied_groups=eff.denied_groups,
            project_ids=eff.projects,
            source_acl_revision=acl_hash,
            retrieved_at=now,
            owner=eff.owner,
        )


class S3ManifestGovernanceAdapter:
    """Manual labels from the S3 ``_access.yaml``. Use ``PurviewDataMapGovernance`` when Purview scans S3."""

    version = "s3-manual-governance-v1"

    def __init__(self, connector: S3Connector) -> None:
        self._c = connector

    async def get(self, ref: SourceRef) -> GovernanceMetadata:
        _, access = await self._c._access(self._c._collection_prefix(ref.source_key))
        label = (
            effective_access(access, ref.source_key.rsplit("/", 1)[-1]).sensitivity_label if access else None
        )
        return GovernanceMetadata(
            source="manual" if label else "none",
            raw_label_id=label,
            mapped_label=label,
            governance_version=f"{self.version}:{label or 'none'}",
            retrieved_at=datetime.now(UTC),
        )
