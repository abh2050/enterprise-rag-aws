"""Ingestion workflow: idempotent, checkpointed steps.

detect → capture → land → validate → scan → parse → normalize → chunk → attach → embed → stage
  → publish → retire

* Event idempotency: ``events`` table (conditional put on deterministic event id).
* Step checkpoints: ``workflow`` table keyed ``ingest:<document_id>:<version>`` / step. A restarted worker
  skips completed steps and reloads their outputs from the artifact store.
* Publication is a conditional flip of ``current_version`` in DynamoDB; readers recheck against it, so a
  mixed or partial version is never served.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from erp_auth.models import DocPermissionRecord, SensitivityLabel, label_rank
from erp_auth.policy_translation import GovernanceMetadata, SourcePermissions, TranslationPolicy, translate
from erp_connectors.artifacts import ArtifactStore
from erp_connectors.base import ChangeEvent, FetchedContent, GovernanceAdapter, SourceConnector, SourceRef
from erp_connectors.chunking import DraftChunk, chunk_document
from erp_connectors.parsing import (
    OcrFn,
    ParsedDocument,
    ProtectedContentError,
    ValidationFailure,
    parse_document,
    validate_file,
)
from erp_connectors.scanning import MalwareScanner
from erp_observability.audit import AuditLog
from erp_observability.tracing import get_tracer, trace_context
from erp_rag import ids
from erp_rag.gateway.gateway import ModelGateway
from erp_rag.schemas import EXTRACTION_VERSION, ChunkRecord, PublicationStatus
from erp_rag.search.opensearch import ChunkIndex
from erp_rag.stores.dynamo import JsonTable, PermissionStore
from erp_rag.text import extract_identifiers

log = logging.getLogger("erp.ingestion")

EVENT_LEASE_SECONDS = 600
EMBED_BATCH = 32


@dataclass
class IngestResult:
    event_id: str
    outcome: Literal[
        "published", "duplicate", "unchanged", "quarantined", "deleted", "permissions_updated", "failed"
    ]
    document_id: str | None = None
    document_version: str | None = None
    detail: str | None = None
    chunks: int = 0
    coverage: dict[str, Any] | None = None


class QuarantineError(Exception):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class IngestionPipeline:
    connectors: dict[str, SourceConnector]
    governance: dict[str, GovernanceAdapter]
    artifacts: ArtifactStore
    scanner: MalwareScanner
    gateway: ModelGateway
    index: ChunkIndex
    store: PermissionStore
    events: JsonTable
    checkpoints: JsonTable
    manifests: JsonTable
    connector_state: JsonTable
    audit: AuditLog
    max_file_bytes: int = 50 * 1024 * 1024
    min_coverage: float = 1.0
    ocr: OcrFn | None = None
    translation: TranslationPolicy = field(default_factory=TranslationPolicy)
    # Test hook: called before each step; raising simulates a worker crash at that step.
    fault_hook: Callable[[str], Awaitable[None]] | None = None
    step_runs: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ public entry points

    async def sync(self, connector_name: str) -> list[IngestResult]:
        """Detect changes for a connector and process them (local orchestrator)."""
        connector = self.connectors[connector_name]
        state = await self.connector_state.get({"connector_id": connector_name}) or {}
        events, cursor = await connector.list_changes(state.get("cursor", {}))
        results = [await self.handle(e) for e in events]
        # Advance the cursor only for keys whose events succeeded; failed keys are retried next sync.
        failed_keys = {
            e.ref.source_key for e, r in zip(events, results, strict=True) if r.outcome == "failed"
        }
        old = state.get("cursor", {})
        merged = {k: (old.get(k, "") if k in failed_keys else v) for k, v in cursor.items()}
        merged = {k: v for k, v in merged.items() if v}
        await self.connector_state.put({"connector_id": connector_name}, {"cursor": merged, "at": _now()})
        return results

    async def handle(self, event: ChangeEvent, *, force: bool = False) -> IngestResult:
        with trace_context(event.trace_id), get_tracer().span("ingest.event", kind=event.kind) as span:
            eid = event.event_id
            if not force and not await self._claim_event(eid, event):
                span.set(duplicate=True)
                return IngestResult(event_id=eid, outcome="duplicate")
            try:
                if event.kind == "delete":
                    result = await self._delete(event)
                elif event.kind == "permissions":
                    result = await self._permissions(event)
                else:
                    result = await self._upsert(event)
            except Exception as exc:
                log.warning("ingestion failed: %s", type(exc).__name__)
                await self.events.put(
                    {"event_id": eid},
                    {
                        "status": "failed",
                        "error": type(exc).__name__,
                        "event": event.model_dump(mode="json"),
                        "at": _now(),
                    },
                )
                span.fail(type(exc).__name__)
                return IngestResult(event_id=eid, outcome="failed", detail=type(exc).__name__)
            await self.events.put(
                {"event_id": eid},
                {
                    "status": "done",
                    "outcome": result.outcome,
                    "event": event.model_dump(mode="json"),
                    "at": _now(),
                },
            )
            span.set(
                outcome=result.outcome,
                document_id=result.document_id,
                version=result.document_version,
                chunks=result.chunks,
            )
            return result

    async def replay_failed(self, event_id: str) -> IngestResult:
        entry = await self.events.get({"event_id": event_id})
        if entry is None:
            raise KeyError(event_id)
        return await self.handle(ChangeEvent.model_validate(entry["event"]), force=True)

    async def replay_document(self, tenant_id: str, document_id: str, *, actor: str) -> IngestResult:
        """Re-run ingestion for a quarantined/failed document from its source."""
        manifest = await self.manifests.get(
            {"document_id": document_id, "document_version": "latest-attempt"}
        )
        if manifest is None or manifest.get("tenant_id") != tenant_id:
            raise KeyError(document_id)
        ref = SourceRef.model_validate(manifest["ref"])
        event = ChangeEvent(
            kind="upsert", ref=ref, source_revision=f"replay:{_now()}", detected_at=datetime.now(UTC)
        )
        self.audit.record(
            "ingest.replay", actor=actor, tenant_id=tenant_id, outcome="started", resource=document_id
        )
        return await self.handle(event, force=True)

    async def reconcile(self, connector_name: str, tenant_id: str) -> list[IngestResult]:
        """Periodic reconciliation: delete records whose source object no longer exists."""
        connector = self.connectors[connector_name]
        live = {ids.document_id(r.tenant_id, r.source, r.source_key) for r in await connector.list_all()}
        results: list[IngestResult] = []
        for rec in await self.store.list_tenant(tenant_id):
            if rec.status == "deleted" or not rec.source_uri.startswith(_uri_prefix(connector_name)):
                continue
            if rec.document_id not in live:
                manifest = await self.manifests.get(
                    {"document_id": rec.document_id, "document_version": "latest-attempt"}
                )
                if manifest is None:
                    continue
                ref = SourceRef.model_validate(manifest["ref"])
                ev = ChangeEvent(
                    kind="delete", ref=ref, source_revision="reconcile", detected_at=datetime.now(UTC)
                )
                results.append(await self.handle(ev, force=True))
        return results

    # ------------------------------------------------------------------ idempotency

    async def _claim_event(self, eid: str, event: ChangeEvent) -> bool:
        claimed = await self.events.put_if_absent(
            {"event_id": eid},
            {
                "status": "processing",
                "event": event.model_dump(mode="json"),
                "at": _now(),
                "lease_until": time.time() + EVENT_LEASE_SECONDS,
            },
        )
        if claimed:
            return True
        existing = await self.events.get({"event_id": eid}) or {}
        status = existing.get("status")
        if status == "done":
            return False
        if status == "processing" and float(existing.get("lease_until", 0)) > time.time():
            return False  # another worker holds the lease
        # failed, or a processing lease expired (worker died) → take over; checkpoints make this safe.
        await self.events.put(
            {"event_id": eid},
            {**existing, "status": "processing", "lease_until": time.time() + EVENT_LEASE_SECONDS},
        )
        return True

    async def _step(self, run: str, step: str) -> dict[str, Any] | None:
        return await self.checkpoints.get({"run_id": run, "step": step})

    async def _done(self, run: str, step: str, data: dict[str, Any]) -> None:
        await self.checkpoints.put({"run_id": run, "step": step}, {**data, "at": _now()})

    async def _enter(self, step: str) -> None:
        self.step_runs[step] = self.step_runs.get(step, 0) + 1
        if self.fault_hook is not None:
            await self.fault_hook(step)

    # ------------------------------------------------------------------ upsert

    async def _upsert(self, event: ChangeEvent) -> IngestResult:
        connector = self.connectors[event.ref.source]
        governance_adapter = self.governance.get(event.ref.source)
        await self._enter("capture")
        content = await connector.fetch(event.ref)
        permissions = await connector.get_permissions(event.ref)
        governance = (
            await governance_adapter.get(event.ref)
            if governance_adapter
            else GovernanceMetadata(source="none")
        )
        tenant = permissions.tenant_id
        doc_id = ids.document_id(tenant, event.ref.source, event.ref.source_key)
        checksum = ids.content_checksum(content.data)
        version = ids.document_version(content.source_revision, checksum)
        run = f"ingest:{doc_id}:{version}"
        eid = event.event_id
        existing = await self.store.get(tenant, doc_id)
        await self.manifests.put(
            {"document_id": doc_id, "document_version": "latest-attempt"},
            {
                "tenant_id": tenant,
                "ref": event.ref.model_dump(),
                "version": version,
                "event_id": eid,
                "at": _now(),
            },
        )

        if existing and existing.current_version == version and existing.status == "published":
            # Same content already published: only permissions/governance may have changed.
            changed = await self._apply_permissions(existing, permissions, governance, content)
            return IngestResult(
                event_id=eid,
                outcome="permissions_updated" if changed else "unchanged",
                document_id=doc_id,
                document_version=version,
            )

        try:
            if governance.protected_content:
                raise QuarantineError("protected_content")
            landing_key = f"landing/{tenant}/{doc_id}/{version}/{_safe_name(content.filename)}"
            if not await self._step(run, "land"):
                await self._enter("land")
                await self.artifacts.put(landing_key, content.data, legal_hold=content.legal_hold)
                await self._done(run, "land", {"key": landing_key})

            if not await self._step(run, "validate"):
                await self._enter("validate")
                try:
                    media_type = validate_file(content.data, content.filename, max_bytes=self.max_file_bytes)
                except ProtectedContentError as exc:
                    raise QuarantineError("protected_content") from exc
                except ValidationFailure as exc:
                    raise QuarantineError(f"validation:{exc.code}") from exc
                await self._done(run, "validate", {"media_type": media_type})
            media_type = (await self._step(run, "validate") or {})["media_type"]

            if not await self._step(run, "scan"):
                await self._enter("scan")
                result = await self.scanner.scan(content.data, artifact_key=landing_key)
                if not result.clean:
                    raise QuarantineError(f"malware_scan:{result.status}", {"engine": result.engine})
                await self._done(run, "scan", {"engine": result.engine, "status": result.status})

            parsed_key = f"parsed/{tenant}/{doc_id}/{version}/parsed.json"
            if not await self._step(run, "parse"):
                await self._enter("parse")
                try:
                    parsed = await parse_document(content.data, media_type, ocr=self.ocr)
                except ProtectedContentError as exc:
                    raise QuarantineError("protected_content") from exc
                except ValidationFailure as exc:
                    raise QuarantineError(f"parse:{exc.code}") from exc
                if parsed.coverage_ratio < self.min_coverage or not parsed.blocks:
                    raise QuarantineError("extraction_incomplete", parsed.coverage_report())
                await self.artifacts.put(parsed_key, parsed.model_dump_json().encode())
                await self._done(run, "parse", {"key": parsed_key, "coverage": parsed.coverage_report()})
            parsed = ParsedDocument.model_validate_json((await self.artifacts.get(parsed_key)) or b"{}")
            coverage = (await self._step(run, "parse") or {}).get("coverage")

            await self._enter("chunk")
            drafts = chunk_document(parsed, title=content.title)
            if not drafts:
                raise QuarantineError("no_chunks")

            # Permission record first (staged), so ACL/label are authoritative before anything is searchable.
            record = await self._upsert_record(existing, doc_id, permissions, governance, content)
            label = record.sensitivity_label
            chunks = self._attach(drafts, record, doc_id, version, content, governance)

            if not await self._step(run, "stage"):
                await self._enter("embed")
                vectors: list[list[float]] = []
                classification = (
                    SensitivityLabel(label) if label_rank(label) is not None else SensitivityLabel.RESTRICTED
                )
                for i in range(0, len(chunks), EMBED_BATCH):
                    batch = chunks[i : i + EMBED_BATCH]
                    res = await self.gateway.embed(
                        [f"{c.title}\n{c.section}\n{c.content}" for c in batch],
                        purpose="document",
                        classification=classification,
                    )
                    vectors.extend(res.value)
                await self._enter("stage")
                await self.index.index_chunks(chunks, vectors)
                await self.manifests.put(
                    {"document_id": doc_id, "document_version": version},
                    {
                        "tenant_id": tenant,
                        "chunk_ids": [c.chunk_id for c in chunks],
                        "coverage": coverage,
                        "landing_key": landing_key,
                        "filename": content.filename,
                        "status": "staged",
                        "extraction_version": EXTRACTION_VERSION,
                        "embedding_version": self.index.embedding_version,
                        "source_revision": content.source_revision,
                    },
                )
                await self._done(run, "stage", {"chunks": len(chunks)})

            await self._enter("publish")
            expected = existing.current_version if existing else None
            fresh = await self.store.get(tenant, doc_id)
            expected = fresh.current_version if fresh else expected
            if expected != version:
                ok = await self.store.publish_version(
                    tenant, doc_id, new_version=version, expected_version=expected
                )
                if not ok:
                    raise RuntimeError("publish race lost; event will be retried")
            await self.index.set_version_status(doc_id, version, PublicationStatus.PUBLISHED)

            await self._enter("retire")
            retired = await self.index.retire_other_versions(doc_id, version)
            if expected and expected != version:
                old = await self.manifests.get({"document_id": doc_id, "document_version": expected})
                if old is not None:
                    await self.manifests.put(
                        {"document_id": doc_id, "document_version": expected},
                        {**old, "status": "retired", "retired_at": _now()},
                    )
            await self.manifests.put(
                {"document_id": doc_id, "document_version": version},
                {
                    **(await self.manifests.get({"document_id": doc_id, "document_version": version}) or {}),
                    "status": "published",
                    "published_at": _now(),
                },
            )
            await self._done(run, "publish", {"retired_chunks": retired})
        except QuarantineError as q:
            await self._quarantine(existing, tenant, doc_id, version, permissions, governance, content, q)
            return IngestResult(
                event_id=eid,
                outcome="quarantined",
                document_id=doc_id,
                document_version=version,
                detail=q.reason,
                coverage=q.detail or None,
            )

        self.audit.record(
            "ingest.publish",
            actor="ingestion",
            tenant_id=tenant,
            outcome="published",
            resource=doc_id,
            version=version,
            chunks=len(chunks),
            previous=expected,
        )
        return IngestResult(
            event_id=eid,
            outcome="published",
            document_id=doc_id,
            document_version=version,
            chunks=len(chunks),
            coverage=coverage,
        )

    def _attach(
        self,
        drafts: list[DraftChunk],
        record: DocPermissionRecord,
        doc_id: str,
        version: str,
        content: FetchedContent,
        governance: GovernanceMetadata,
    ) -> list[ChunkRecord]:
        chunk_ids = [
            ids.chunk_id(doc_id, version, EXTRACTION_VERSION, d.ordinal, ids.content_checksum(d.content))
            for d in drafts
        ]
        out: list[ChunkRecord] = []
        for i, d in enumerate(drafts):
            prev_id = chunk_ids[i - 1] if i > 0 and drafts[i - 1].section_id == d.section_id else None
            next_id = (
                chunk_ids[i + 1] if i + 1 < len(drafts) and drafts[i + 1].section_id == d.section_id else None
            )
            out.append(
                ChunkRecord(
                    tenant_id=record.tenant_id,
                    document_id=doc_id,
                    document_version=version,
                    chunk_id=chunk_ids[i],
                    source_uri=content.source_uri,
                    title=content.title,
                    page=d.page,
                    page_end=d.page_end,
                    location=d.location,
                    section=d.section,
                    content=d.content,
                    content_checksum=ids.content_checksum(d.content),
                    source_modified_at=content.source_modified_at,
                    extraction_version=EXTRACTION_VERSION,
                    embedding_version=self.index.embedding_version,
                    allowed_principals=sorted(record.allowed_principals),
                    denied_principals=sorted(record.denied_principals),
                    project_ids=sorted(record.project_ids),
                    project_restricted=bool(record.project_ids),
                    sensitivity_label=record.sensitivity_label,
                    acl_version=record.acl_version,
                    governance_version=governance.governance_version,
                    publication_status=PublicationStatus.STAGED,
                    ordinal=d.ordinal,
                    chunk_type="table" if d.chunk_type == "table" else "text",
                    table_header=d.table_header,
                    identifiers=extract_identifiers(d.content)[:50],
                    prev_chunk_id=prev_id,
                    next_chunk_id=next_id,
                    parent_section_id=d.section_id,
                    series_id=content.series_id,
                    effective_date=content.effective_date,
                )
            )
        return out

    async def _upsert_record(
        self,
        existing: DocPermissionRecord | None,
        doc_id: str,
        permissions: SourcePermissions,
        governance: GovernanceMetadata,
        content: FetchedContent,
    ) -> DocPermissionRecord:
        if existing is None or existing.status in ("deleted",):
            record = translate(
                document_id=doc_id,
                permissions=permissions,
                governance=governance,
                status="staged",
                current_version=None,
                acl_version=(existing.acl_version + 1) if existing else 1,
                title=content.title,
                source_uri=content.source_uri,
                source_modified_at=content.source_modified_at,
                legal_hold=content.legal_hold,
                policy=self.translation,
            )
            await self.store.put(record)
            return record
        await self._apply_permissions(existing, permissions, governance, content)
        fresh = await self.store.get(existing.tenant_id, doc_id)
        assert fresh is not None
        return fresh

    async def _apply_permissions(
        self,
        existing: DocPermissionRecord,
        permissions: SourcePermissions,
        governance: GovernanceMetadata,
        content: FetchedContent | None,
    ) -> bool:
        """Write the authoritative ACL/label first (immediate effect), then propagate to the index."""
        target = translate(
            document_id=existing.document_id,
            permissions=permissions,
            governance=governance,
            status=existing.status,
            current_version=existing.current_version,
            acl_version=existing.acl_version,
            title=content.title if content else existing.title,
            source_uri=content.source_uri if content else existing.source_uri,
            source_modified_at=content.source_modified_at if content else existing.source_modified_at,
            legal_hold=content.legal_hold if content else existing.legal_hold,
            policy=self.translation,
        )
        acl_fields = {
            "allowed_principals": target.allowed_principals,
            "denied_principals": target.denied_principals,
            "project_ids": target.project_ids,
            "sensitivity_label": target.sensitivity_label,
            "acl_complete": target.acl_complete,
            "governance_version": target.governance_version,
            "governance_source": target.governance_source,
        }
        changed = (
            any(getattr(existing, k) != v for k, v in acl_fields.items())
            or existing.tenant_id != target.tenant_id
        )
        meta = {
            "acl_synced_at": permissions.retrieved_at,
            "title": target.title,
            "source_uri": target.source_uri,
            "source_modified_at": target.source_modified_at,
            "legal_hold": target.legal_hold,
        }
        updated = await self.store.update_fields(
            existing.tenant_id, existing.document_id, {**acl_fields, **meta}, bump_acl=changed
        )
        if changed:
            await self.index.update_acl(
                existing.document_id,
                {
                    "allowed_principals": sorted(updated.allowed_principals),
                    "denied_principals": sorted(updated.denied_principals),
                    "project_ids": sorted(updated.project_ids),
                    "project_restricted": bool(updated.project_ids),
                    "sensitivity_label": updated.sensitivity_label,
                    "acl_version": updated.acl_version,
                    "governance_version": updated.governance_version,
                },
            )
            self.audit.record(
                "acl.update",
                actor="ingestion",
                tenant_id=existing.tenant_id,
                outcome="updated",
                resource=existing.document_id,
                acl_version=updated.acl_version,
            )
        return changed

    # -------------------------------------------------------- permissions-only / delete / quarantine

    async def _permissions(self, event: ChangeEvent) -> IngestResult:
        connector = self.connectors[event.ref.source]
        await self._enter("permissions")
        permissions = await connector.get_permissions(event.ref)
        adapter = self.governance.get(event.ref.source)
        governance = await adapter.get(event.ref) if adapter else GovernanceMetadata(source="none")
        doc_id = ids.document_id(permissions.tenant_id, event.ref.source, event.ref.source_key)
        existing = await self.store.get(permissions.tenant_id, doc_id)
        if existing is None:
            # Tenant reassignment or unseen document: treat as a full upsert.
            return await self._upsert(event.model_copy(update={"kind": "upsert"}))
        changed = await self._apply_permissions(existing, permissions, governance, None)
        return IngestResult(
            event_id=event.event_id,
            outcome="permissions_updated" if changed else "unchanged",
            document_id=doc_id,
            document_version=existing.current_version,
        )

    async def _delete(self, event: ChangeEvent) -> IngestResult:
        tenant = event.ref.tenant_id
        doc_id = ids.document_id(tenant, event.ref.source, event.ref.source_key)
        existing = await self.store.get(tenant, doc_id)
        if existing is None:
            return IngestResult(event_id=event.event_id, outcome="unchanged", document_id=doc_id)
        await self.tombstone(tenant, doc_id, actor="ingestion", reason="source_deleted")
        return IngestResult(event_id=event.event_id, outcome="deleted", document_id=doc_id)

    async def tombstone(
        self, tenant_id: str, document_id: str, *, actor: str, reason: str
    ) -> DocPermissionRecord:
        """Remove from retrieval immediately; then purge index; keep artifacts under legal hold."""
        record = await self.store.update_fields(
            tenant_id, document_id, {"status": "deleted", "revoked": True}, bump_acl=True
        )
        self.audit.record(
            "document.delete",
            actor=actor,
            tenant_id=tenant_id,
            outcome="tombstoned",
            resource=document_id,
            reason=reason,
            legal_hold=record.legal_hold,
        )
        await self.index.delete_document(document_id)  # background cleanup in AWS; not on the security path
        if not record.legal_hold:
            await self.artifacts.delete_prefix(f"landing/{tenant_id}/{document_id}/")
            await self.artifacts.delete_prefix(f"parsed/{tenant_id}/{document_id}/")
        return record

    async def revoke(
        self, tenant_id: str, document_id: str, *, actor: str, reason: str
    ) -> DocPermissionRecord:
        """Fast revocation: authoritative flag first (effective on next request), index cleanup after."""
        record = await self.store.update_fields(tenant_id, document_id, {"revoked": True}, bump_acl=True)
        self.audit.record(
            "document.revoke",
            actor=actor,
            tenant_id=tenant_id,
            outcome="revoked",
            resource=document_id,
            reason=reason,
            acl_version=record.acl_version,
        )
        await self.index.update_acl(
            document_id, {"allowed_principals": [], "acl_version": record.acl_version}
        )
        return record

    async def _quarantine(
        self,
        existing: DocPermissionRecord | None,
        tenant: str,
        doc_id: str,
        version: str,
        permissions: SourcePermissions,
        governance: GovernanceMetadata,
        content: FetchedContent,
        q: QuarantineError,
    ) -> None:
        landing_prefix = f"landing/{tenant}/{doc_id}/{version}/"
        data = content.data
        await self.artifacts.put(
            f"quarantine/{tenant}/{doc_id}/{version}/{_safe_name(content.filename)}",
            data,
            legal_hold=content.legal_hold,
        )
        await self.artifacts.delete_prefix(landing_prefix)
        await self.manifests.put(
            {"document_id": doc_id, "document_version": version},
            {
                "tenant_id": tenant,
                "status": "quarantined",
                "reason": q.reason,
                "detail": q.detail,
                "at": _now(),
            },
        )
        if existing is None:
            record = translate(
                document_id=doc_id,
                permissions=permissions,
                governance=governance,
                status="quarantined",
                current_version=None,
                acl_version=1,
                title=content.title,
                source_uri=content.source_uri,
                source_modified_at=content.source_modified_at,
                legal_hold=content.legal_hold,
                policy=self.translation,
            )
            await self.store.put(record)
        # If an earlier version is published it stays published (it passed all checks); the failed new
        # version is quarantined and visible to admins via the manifest.
        self.audit.record(
            "ingest.quarantine",
            actor="ingestion",
            tenant_id=tenant,
            outcome="quarantined",
            resource=doc_id,
            reason=q.reason,
            version=version,
        )


def _safe_name(filename: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in filename)[:200] or "file"


def _uri_prefix(connector_name: str) -> str:
    """Source URI prefix owned by a connector; reconciliation never touches other connectors' records."""
    if connector_name.startswith("localfs"):
        return f"file://{connector_name}/"
    return f"{connector_name}://"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)
