"""Narrow connector contracts.

New sources implement ``SourceConnector`` (and optionally ``GovernanceAdapter``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from erp_auth.policy_translation import GovernanceMetadata, SourcePermissions
from erp_rag.ids import event_id


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str  # connector name, e.g. "localfs:demo", "s3:bucket", "sharepoint:site"
    source_key: str  # stable key within the source (path, object key, drive item id)
    tenant_id: str


class ChangeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["upsert", "delete", "permissions"]
    ref: SourceRef
    source_revision: str
    detected_at: datetime
    trace_id: str | None = None

    @property
    def event_id(self) -> str:
        return event_id(
            self.kind, self.ref.tenant_id, self.ref.source, self.ref.source_key, self.source_revision
        )


class FetchedContent(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: SourceRef
    filename: str
    data: bytes
    source_revision: str
    source_modified_at: datetime | None
    source_uri: str
    title: str
    owner: str | None = None
    series_id: str | None = None
    effective_date: datetime | None = None
    legal_hold: bool = False


class SourceConnector(Protocol):
    name: str

    async def list_changes(self, cursor: dict[str, str]) -> tuple[list[ChangeEvent], dict[str, str]]:
        """Return changes since ``cursor`` and the new cursor (opaque per connector)."""
        ...

    async def fetch(self, ref: SourceRef) -> FetchedContent: ...

    async def get_permissions(self, ref: SourceRef) -> SourcePermissions: ...

    async def list_all(self) -> list[SourceRef]:
        """Full listing used by periodic reconciliation."""
        ...


class GovernanceAdapter(Protocol):
    version: str

    async def get(self, ref: SourceRef) -> GovernanceMetadata: ...


class ConnectorError(Exception):
    pass
