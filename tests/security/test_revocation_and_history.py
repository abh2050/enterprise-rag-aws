"""Revocation fast path, cached answers, conversation-history leakage, stale ACLs, client-supplied input.

Uses the HTTP API with real OpenSearch + DynamoDB Local. Revocation is applied to the authoritative
record only; the tests prove access is blocked *before* any index cleanup is relied upon.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from erp_evals import fixtures
from erp_ingestion.pipeline import IngestionPipeline
from erp_rag.runtime import Core
from tests.conftest import Users
from tests.integration.helpers import ALICE, access, temp_source, unique_term

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


async def _ask(client: httpx.AsyncClient, users: Users, who: str, q: str, conv: str | None = None) -> dict:
    body: dict[str, str] = {"question": q}
    if conv:
        body["conversation_id"] = conv
    r = await client.post("/api/ask", json=body, headers=await users.headers(who))
    assert r.status_code == 200, r.text
    return r.json()


async def _publish(
    pipeline: IngestionPipeline, tmp_path: Path, text: str, groups: list[str]
) -> tuple[str, str]:
    fixtures.write_collection(tmp_path, "c", {"doc.md": text}, access(groups))
    ctx = temp_source(pipeline, tmp_path)
    name, _ = ctx.__enter__()
    result = (await pipeline.sync(name))[0]
    assert result.outcome == "published" and result.document_id
    return result.document_id, name


async def test_revocation_blocks_retrieval_cache_history_and_citations(
    client: httpx.AsyncClient, users: Users, core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    doc, _ = await _publish(
        pipeline, tmp_path, f"# Bonus\n\nThe {term} bonus pool is 2 million USD.\n", ["acme-finance"]
    )
    q = f"How large is the {term} bonus pool?"
    first = await _ask(client, users, "alice@acme.example", q, conv="conv-revoke")
    assert any(c["document_id"] == doc for c in first["citations"])
    cached = await _ask(client, users, "alice@acme.example", q)
    assert cached["from_cache"] and any(c["document_id"] == doc for c in cached["citations"])
    citation_url = next(c["open_url"] for c in first["citations"] if c["document_id"] == doc)

    # Fast path: authoritative record only (no index update awaited by this test).
    t0 = time.perf_counter()
    await core.store.update_fields(ALICE.tenant_id, doc, {"revoked": True}, bump_acl=True)
    after = await _ask(client, users, "alice@acme.example", q)
    revocation_delay_ms = (time.perf_counter() - t0) * 1000

    assert not after["from_cache"], "cached answer must not survive revocation"
    assert all(c["document_id"] != doc for c in after["citations"])
    assert all("2 million" not in c["text"] for c in after["claims"])
    hdr = await users.headers("alice@acme.example")
    assert (await client.get(citation_url, headers=hdr)).status_code == 404
    assert (await client.get(f"/api/documents/{doc}/download", headers=hdr)).status_code == 404
    history = (await client.get("/api/conversations/conv-revoke", headers=hdr)).json()
    assert history["turns"][0]["withheld"] is True
    assert "2 million" not in history["turns"][0]["answer_text"]
    # Search index still holds the stale ACL copy at this point — the recheck is what blocked access.
    resp = await core.index.client.count(
        index=core.index.name, body={"query": {"term": {"document_id": doc}}}
    )
    assert resp["count"] >= 1
    print(f"\nREVOCATION_DELAY_MS={revocation_delay_ms:.1f} (revoke write → first denied answer, local)")


async def test_history_is_private_and_revalidated_on_reuse(
    client: httpx.AsyncClient, users: Users, core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    doc, _ = await _publish(
        pipeline, tmp_path, f"# Codes\n\nThe {term} vault code rotates every 9 days.\n", ["acme-finance"]
    )
    first = await _ask(
        client,
        users,
        "alice@acme.example",
        f"How often does the {term} vault code rotate?",
        conv="conv-private",
    )
    assert any(c["document_id"] == doc for c in first["citations"])
    # Another user addressing the same conversation id gets their own (empty) namespace.
    bob_view = (
        await client.get("/api/conversations/conv-private", headers=await users.headers("bob@acme.example"))
    ).json()
    assert bob_view["turns"] == []
    bob_answer = await _ask(
        client, users, "bob@acme.example", "And how often does it rotate?", conv="conv-private"
    )
    assert all(c["document_id"] != doc for c in bob_answer["citations"])

    # Access changes → the earlier turn is withheld and not reused for follow-ups.
    await core.store.update_fields(
        ALICE.tenant_id, doc, {"allowed_principals": ["group:acme-engineering"]}, bump_acl=True
    )
    follow = await _ask(client, users, "alice@acme.example", "And who owns it?", conv="conv-private")
    assert all(c["document_id"] != doc for c in follow["citations"])
    turns = (
        await client.get("/api/conversations/conv-private", headers=await users.headers("alice@acme.example"))
    ).json()["turns"]
    assert turns[0]["withheld"] and "9 days" not in turns[0]["answer_text"]


async def test_stale_acl_denies(
    client: httpx.AsyncClient, users: Users, core: Core, pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    term = unique_term()
    doc, _ = await _publish(
        pipeline, tmp_path, f"# Stale\n\nThe {term} quota is 40 seats.\n", ["acme-finance"]
    )
    q = f"What is the {term} quota?"
    assert any(
        c["document_id"] == doc for c in (await _ask(client, users, "alice@acme.example", q))["citations"]
    )
    stale = datetime.now(UTC) - timedelta(seconds=core.policy.max_acl_age_seconds + 60)
    await core.store.update_fields(ALICE.tenant_id, doc, {"acl_synced_at": stale})
    again = await _ask(client, users, "alice@acme.example", q + " now")
    assert all(c["document_id"] != doc for c in again["citations"])


async def test_client_supplied_authorization_inputs_are_ignored(
    client: httpx.AsyncClient, users: Users
) -> None:
    hdr = await users.headers("bob@acme.example")
    q = "What is the per diem for New York under the travel and expense policy?"
    for extra in (
        {"tenant_id": "x"},
        {"groups": ["acme-finance"]},
        {"filters": {"document_id": "x"}},
        {"clearance": "restricted"},
    ):
        r = await client.post("/api/ask", json={"question": q, **extra}, headers=hdr)
        assert r.status_code == 422, extra
    spoof = {
        **hdr,
        "X-Tenant-Id": "11111111-1111-4111-8111-111111111111",
        "X-Groups": "acme-finance",
        "X-Clearance": "restricted",
    }
    r = await client.post("/api/ask", json={"question": q}, headers=spoof)
    assert r.status_code == 200
    assert all("Expense" not in c["title"] for c in r.json()["citations"])


async def test_admin_actions_require_admin_role_and_same_tenant(
    client: httpx.AsyncClient, users: Users
) -> None:
    alice = await users.headers("alice@acme.example")
    assert (await client.get("/api/admin/documents", headers=alice)).status_code == 403
    erin = await users.headers("erin@acme.example")
    listing = (await client.get("/api/admin/documents", headers=erin)).json()["documents"]
    assert listing and all(d["document_id"].startswith("doc_") for d in listing)
    carol_doc_titles = {d["title"] for d in listing}
    assert "Globex Expense Guidelines" not in carol_doc_titles  # admin scope is the admin's own tenant
