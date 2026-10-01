"""Local file connector + manual governance adapter.

Layout: ``<root>/<collection>/_access.yaml`` declares the *source permission record* for every file in
that collection (with optional per-file overrides). A file with no valid ``_access.yaml`` yields an
empty grant set, which the policy engine denies (and the pipeline quarantines).

Labels declared here are **manual governance** (``governance_source=manual``) — never Purview.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from erp_auth.policy_translation import GovernanceMetadata, SourcePermissions
from erp_connectors.access_manifest import (
    ACCESS_FILE,
    SUPPORTED_SUFFIXES,
    AccessFile,
    effective_access,
    parse_access,
)
from erp_connectors.base import ChangeEvent, ConnectorError, FetchedContent, SourceRef


class LocalFileConnector:
    def __init__(self, root: Path, *, name: str = "localfs") -> None:
        self.root = root.resolve()
        self.name = name

    # ------------------------------------------------------------------ helpers

    def _collections(self) -> list[Path]:
        return sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def _load_access(self, collection: Path) -> AccessFile | None:
        path = collection / ACCESS_FILE
        if not path.exists():
            return None
        return parse_access(path.read_text())

    def _files(self) -> list[tuple[Path, AccessFile | None]]:
        out: list[tuple[Path, AccessFile | None]] = []
        for collection in self._collections():
            access = self._load_access(collection)
            for f in sorted(collection.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES and not f.name.startswith("_"):
                    out.append((f, access))
        return out

    def _ref(self, path: Path, access: AccessFile | None) -> SourceRef:
        tenant = access.tenant_id if access else "unassigned"
        return SourceRef(source=self.name, source_key=str(path.relative_to(self.root)), tenant_id=tenant)

    def _path(self, ref: SourceRef) -> Path:
        path = (self.root / ref.source_key).resolve()
        if self.root not in path.parents:
            raise ConnectorError("path escapes connector root")
        return path

    @staticmethod
    def _content_revision(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:24]

    def _acl_revision(self, path: Path, access: AccessFile | None) -> str:
        eff = effective_access(access, path.name).model_dump(mode="json") if access else {}
        material = json.dumps(
            {"tenant": access.tenant_id if access else None, **eff}, sort_keys=True, default=str
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ SourceConnector

    async def list_all(self) -> list[SourceRef]:
        return [self._ref(p, a) for p, a in self._files()]

    async def list_changes(self, cursor: dict[str, str]) -> tuple[list[ChangeEvent], dict[str, str]]:
        """Cursor maps source_key → "<content_rev>:<acl_rev>:<tenant_id>".

        The tenant is kept so a delete can still be attributed after the file disappears.
        """
        now = datetime.now(UTC)
        events: list[ChangeEvent] = []
        new_cursor: dict[str, str] = {}
        for path, access in self._files():
            ref = self._ref(path, access)
            content_rev, acl_rev = self._content_revision(path), self._acl_revision(path, access)
            new_cursor[ref.source_key] = f"{content_rev}:{acl_rev}:{ref.tenant_id}"
            previous = cursor.get(ref.source_key, "").split(":")
            if len(previous) < 3 or previous[0] != content_rev:
                events.append(
                    ChangeEvent(kind="upsert", ref=ref, source_revision=content_rev, detected_at=now)
                )
            elif previous[1] != acl_rev or previous[2] != ref.tenant_id:
                events.append(
                    ChangeEvent(kind="permissions", ref=ref, source_revision=acl_rev, detected_at=now)
                )
        for key, value in cursor.items():
            if key not in new_cursor:
                parts = value.split(":")
                tenant = parts[2] if len(parts) >= 3 else "unassigned"
                ref = SourceRef(source=self.name, source_key=key, tenant_id=tenant)
                events.append(ChangeEvent(kind="delete", ref=ref, source_revision="deleted", detected_at=now))
        return events, new_cursor

    async def fetch(self, ref: SourceRef) -> FetchedContent:
        path = self._path(ref)
        if not path.exists():
            raise ConnectorError("source file not found")
        access = self._load_access(path.parent)
        eff = effective_access(access, path.name) if access else None
        stat = path.stat()
        eff_date = eff.effective_date if eff else None
        return FetchedContent(
            ref=ref,
            filename=path.name,
            data=path.read_bytes(),
            source_revision=self._content_revision(path),
            source_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            source_uri=f"file://{self.name}/{ref.source_key}",
            title=(eff.title if eff and eff.title else path.stem.replace("-", " ").replace("_", " ").title()),
            owner=eff.owner if eff else None,
            series_id=eff.series_id if eff else None,
            effective_date=datetime(eff_date.year, eff_date.month, eff_date.day, tzinfo=UTC)
            if eff_date
            else None,
            legal_hold=eff.legal_hold if eff else False,
        )

    async def get_permissions(self, ref: SourceRef) -> SourcePermissions:
        path = self._path(ref)
        access = self._load_access(path.parent)
        now = datetime.now(UTC)
        if access is None:
            # Missing/invalid permission file → no grants and an explicit unsupported marker → deny.
            return SourcePermissions(
                tenant_id=ref.tenant_id,
                unsupported_grants=["missing_or_invalid_access_file"],
                source_acl_revision="none",
                retrieved_at=now,
            )
        eff = effective_access(access, path.name)
        return SourcePermissions(
            tenant_id=access.tenant_id,
            allowed_users=eff.allowed_users,
            allowed_groups=eff.allowed_groups,
            denied_users=eff.denied_users,
            denied_groups=eff.denied_groups,
            project_ids=eff.projects,
            source_acl_revision=self._acl_revision(path, access),
            retrieved_at=now,
            owner=eff.owner,
        )


class ManualGovernanceAdapter:
    """Labels declared in ``_access.yaml``. Always reported as ``source='manual'``."""

    version = "manual-governance-v1"

    def __init__(self, connector: LocalFileConnector) -> None:
        self._connector = connector

    async def get(self, ref: SourceRef) -> GovernanceMetadata:
        path = self._connector._path(ref)
        access = self._connector._load_access(path.parent)
        label = effective_access(access, path.name).sensitivity_label if access else None
        return GovernanceMetadata(
            source="manual" if label else "none",
            raw_label_id=str(label) if label else None,
            mapped_label=str(label) if label else None,
            governance_version=f"{self.version}:{label or 'none'}",
            retrieved_at=datetime.now(UTC),
        )
