"""STAGE 2 GATE — ingestion lifecycle against real OpenSearch + DynamoDB Local.

Covers duplicate events, worker restart (crash mid-pipeline + expired lease), version replacement,
deletion (with and without legal hold), permission-only changes, quarantine and replay, reconciliation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from erp_auth.policy import decide
from erp_connectors.base import ChangeEvent
from erp_evals import fixtures
from erp_ingestion.pipeline import IngestionPipeline
from erp_rag.runtime import Core
from tests.integration.helpers import ALICE, BOB, access, doc_id_for, temp_source, unique_term

pytestmark = pytest.mark.integration


async def chunks_for(core: Core, document_id: str, version: str | None = None) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [{"term": {"document_id": document_id}}]
    if version:
        filters.append({"term": {"document_version": version}})
    resp = await core.index.client.search(
        index=core.index.name, body={"size": 200, "query": {"bool": {"filter": filters}}}
    )
    return [h["_source"] for h in resp["hits"]["hits"]]


async def ask_cites(core: Core, ctx, question: str, document_id: str) -> tuple[bool, Any]:  # type: ignore[no-untyped-def]
    resp = await core.qa.ask(ctx, question)
    return any(c.document_id == document_id for c in resp.citations), resp


async def test_duplicate_events_are_idempotent(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    fixtures.write_collection(
        tmp_path, "c", {"memo.md": f"# Memo\n\nThe {term} budget is 500 USD.\n"}, access(["acme-finance"])
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        first = await pipeline.sync(name)
        assert [r.outcome for r in first] == ["published"]
        doc = first[0].document_id
        assert doc is not None
        before = await chunks_for(core, doc)
        events, _ = await pipeline.connectors[name].list_changes({})  # replay the same detection
        again = [await pipeline.handle(e) for e in events]
        assert [r.outcome for r in again] == ["duplicate"]
        # A forced re-delivery of the same content is a no-op too (same deterministic ids).
        forced = await pipeline.handle(events[0], force=True)
        assert forced.outcome == "unchanged"
        after = await chunks_for(core, doc)
        assert sorted(c["chunk_id"] for c in before) == sorted(c["chunk_id"] for c in after)
        assert (await pipeline.sync(name)) == []  # cursor advanced: nothing new to detect


async def test_worker_restart_resumes_from_checkpoints(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    fixtures.write_collection(
        tmp_path, "c", {"guide.md": f"# Guide\n\nThe {term} limit is 7 days.\n"}, access(["acme-finance"])
    )
    crashed = {"done": False}

    async def crash_once(step: str) -> None:
        if step == "embed" and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("simulated worker crash")

    with temp_source(pipeline, tmp_path) as (name, _):
        pipeline.fault_hook = crash_once
        parse_before = pipeline.step_runs.get("parse", 0)
        try:
            first = await pipeline.sync(name)
            assert [r.outcome for r in first] == ["failed"]
            doc = doc_id_for(name, "c/guide.md")
            record = await core.store.get(ALICE.tenant_id, doc)
            assert record is not None and record.status == "staged"  # nothing searchable yet
            assert not decide(ALICE, record, settings=core.policy).allowed
            second = await pipeline.sync(name)  # cursor was not advanced for the failed key
        finally:
            pipeline.fault_hook = None
        assert [r.outcome for r in second] == ["published"]
        assert pipeline.step_runs["parse"] - parse_before == 1  # parse checkpoint reused after restart
        assert len(await chunks_for(core, doc)) >= 1


async def test_expired_lease_is_taken_over_but_active_lease_is_respected(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    fixtures.write_collection(
        tmp_path, "c", {"a.md": f"# A\n\n{unique_term()} alpha text body.\n"}, access(["acme-finance"])
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        events, _ = await pipeline.connectors[name].list_changes({})
        event: ChangeEvent = events[0]
        key = {"event_id": event.event_id}
        await core.tables["events"].put(
            key,
            {
                "status": "processing",
                "lease_until": time.time() + 600,
                "event": event.model_dump(mode="json"),
            },
        )
        assert (await pipeline.handle(event)).outcome == "duplicate"  # another worker holds the lease
        await core.tables["events"].put(
            key,
            {"status": "processing", "lease_until": time.time() - 1, "event": event.model_dump(mode="json")},
        )
        assert (await pipeline.handle(event)).outcome == "published"  # dead worker's lease expired


async def test_version_replacement_publishes_atomically(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    folder = fixtures.write_collection(
        tmp_path,
        "c",
        {"rates.md": f"# Rates\n\nThe {term} reimbursement rate is 10 percent.\n"},
        access(["acme-finance"]),
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        v1 = (await pipeline.sync(name))[0]
        assert v1.outcome == "published"
        doc = v1.document_id
        assert doc
        cited, resp = await ask_cites(core, ALICE, f"What is the {term} reimbursement rate?", doc)
        assert cited and "10 percent" in resp.claims[0].text

        (folder / "rates.md").write_text(f"# Rates\n\nThe {term} reimbursement rate is 12 percent.\n")
        v2 = (await pipeline.sync(name))[0]
        assert v2.outcome == "published" and v2.document_version != v1.document_version
        record = await core.store.get(ALICE.tenant_id, doc)
        assert record and record.current_version == v2.document_version
        assert await chunks_for(core, doc, v1.document_version) == []  # superseded revision retired
        old_manifest = await core.tables["chunk_manifest"].get(
            {"document_id": doc, "document_version": v1.document_version}
        )
        assert old_manifest and old_manifest["status"] == "retired"
        cited, resp = await ask_cites(core, ALICE, f"What is the {term} reimbursement rate?", doc)
        assert cited and all("10 percent" not in c.text for c in resp.claims)
        assert any("12 percent" in c.text for c in resp.claims)


@pytest.mark.parametrize("legal_hold", [False, True])
async def test_deletion_blocks_access_and_respects_legal_hold(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path, legal_hold: bool
) -> None:
    term = unique_term()
    folder = fixtures.write_collection(
        tmp_path,
        "c",
        {"plan.md": f"# Plan\n\nThe {term} launch date is June 3.\n"},
        access(["acme-finance"], legal_hold=str(legal_hold).lower()),
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        published = (await pipeline.sync(name))[0]
        doc, version = published.document_id, published.document_version
        assert doc and version
        landing = f"landing/{ALICE.tenant_id}/{doc}/{version}/plan.md"
        assert await pipeline.artifacts.exists(landing)

        (folder / "plan.md").unlink()
        deleted = await pipeline.sync(name)
        assert [r.outcome for r in deleted] == ["deleted"]
        record = await core.store.get(ALICE.tenant_id, doc)
        assert record and record.status == "deleted" and record.revoked
        assert not decide(ALICE, record, settings=core.policy).allowed
        assert await chunks_for(core, doc) == []
        cited, _ = await ask_cites(core, ALICE, f"When is the {term} launch date?", doc)
        assert not cited
        # Removal from retrieval is separate from physical retention.
        assert await pipeline.artifacts.exists(landing) is legal_hold


async def test_permission_only_change(core: Core, pipeline: IngestionPipeline, tmp_path: Path) -> None:
    term = unique_term()
    folder = fixtures.write_collection(
        tmp_path,
        "c",
        {"ops.md": f"# Ops\n\nThe {term} on-call rotation changes every Monday.\n"},
        access(["acme-finance"], label="internal"),
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        published = (await pipeline.sync(name))[0]
        doc = published.document_id
        assert doc
        before = await core.store.get(ALICE.tenant_id, doc)
        assert before
        question = f"When does the {term} on-call rotation change?"
        assert (await ask_cites(core, ALICE, question, doc))[0]
        assert not (await ask_cites(core, BOB, question, doc))[0]

        (folder / "_access.yaml").write_text(access(["acme-engineering"], label="internal"))
        changed = await pipeline.sync(name)
        assert [r.outcome for r in changed] == ["permissions_updated"]
        after = await core.store.get(ALICE.tenant_id, doc)
        assert after and after.acl_version == before.acl_version + 1
        assert after.current_version == before.current_version  # content not re-ingested
        assert not (await ask_cites(core, ALICE, question, doc))[0]
        assert (await ask_cites(core, BOB, question, doc))[0]
        indexed = await chunks_for(core, doc)
        assert all(c["allowed_principals"] == ["group:acme-engineering"] for c in indexed)


async def test_quarantine_and_replay(core: Core, pipeline: IngestionPipeline, tmp_path: Path) -> None:
    files = {
        "virus.txt": "harmless words " + fixtures.EICAR_TEXT,
        "scanned.pdf": fixtures.scanned_pdf(),
        "locked.pdf": fixtures.encrypted_pdf(),
        "fake.pdf": b"this is not a pdf",
    }
    fixtures.write_collection(tmp_path, "c", files, access(["acme-finance"]))
    with temp_source(pipeline, tmp_path) as (name, _):
        results = {r.document_id: r for r in await pipeline.sync(name)}
        by_file = {key: results[doc_id_for(name, f"c/{key}")] for key in files}
        assert by_file["virus.txt"].detail == "malware_scan:infected"
        assert by_file["locked.pdf"].detail == "protected_content"
        assert by_file["fake.pdf"].detail.startswith("validation:type_mismatch")  # type: ignore[union-attr]
        scanned = by_file["scanned.pdf"]
        assert scanned.detail == "extraction_incomplete"
        assert scanned.coverage and scanned.coverage["uncovered"][0]["page"] == 2
        for r in by_file.values():
            assert r.outcome == "quarantined"
            record = await core.store.get(ALICE.tenant_id, r.document_id or "")
            assert record and record.status == "quarantined"
            assert not decide(ALICE, record, settings=core.policy).allowed
            assert await chunks_for(core, r.document_id or "") == []

        async def fake_ocr(_pdf: bytes, _page: int) -> str:  # stands in for the Textract adapter
            return "Scanned page: warranty period is 24 months."

        pipeline.ocr = fake_ocr
        try:
            replayed = await pipeline.replay_document(
                ALICE.tenant_id, scanned.document_id or "", actor="test-admin"
            )
        finally:
            pipeline.ocr = None
        assert replayed.outcome == "published"
        assert len(await chunks_for(core, scanned.document_id or "")) >= 1


async def test_reconciliation_removes_vanished_sources(
    core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    folder = fixtures.write_collection(
        tmp_path, "c", {"x.md": f"# X\n\n{unique_term()} reconcile me please.\n"}, access(["acme-finance"])
    )
    with temp_source(pipeline, tmp_path) as (name, _):
        doc = (await pipeline.sync(name))[0].document_id
        assert doc
        (folder / "x.md").unlink()  # missed delete event
        results = await pipeline.reconcile(name, ALICE.tenant_id)
        assert [r.outcome for r in results] == ["deleted"]
        record = await core.store.get(ALICE.tenant_id, doc)
        assert record and record.status == "deleted"
