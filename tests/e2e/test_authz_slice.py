"""STAGE 1 GATE — permission-aware retrieval end to end through the HTTP API.

Real local OpenSearch (BM25 + filtered k-NN) and DynamoDB Local; FIXTURE models (deterministic, not
evidence of answer quality). Proves that one user can retrieve and cite an allowed document while
another user (different group, or different tenant) cannot retrieve it, cite it, or fetch it directly.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import Users

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

QUESTION = "What is the per diem for New York under the travel and expense policy?"
FINANCE_TITLE = "Travel & Expense Policy (2024)"


async def _ask(client: httpx.AsyncClient, users: Users, who: str, question: str = QUESTION) -> dict:
    resp = await client.post("/api/ask", json={"question": question}, headers=await users.headers(who))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_allowed_user_retrieves_and_cites_finance_policy(
    client: httpx.AsyncClient, users: Users
) -> None:
    body = await _ask(client, users, "alice@acme.example")
    assert body["status"] in ("answered", "limited"), body
    titles = {c["title"] for c in body["citations"]}
    assert FINANCE_TITLE in titles
    assert any("95" in claim["text"] for claim in body["claims"]), body["claims"]
    # Every claim cites only citations that were returned (server-resolved).
    cited = {cid for claim in body["claims"] for cid in claim["citation_ids"]}
    assert cited <= {c["citation_id"] for c in body["citations"]}
    assert body["inference_mode"] == "fixture"

    # The allowed user can open the citation and download the source document.
    citation = body["citations"][0]
    opened = await client.get(citation["open_url"], headers=await users.headers("alice@acme.example"))
    assert opened.status_code == 200
    assert opened.json()["document_id"] == citation["document_id"]
    dl = await client.get(
        f"/api/documents/{citation['document_id']}/download",
        headers=await users.headers("alice@acme.example"),
    )
    assert dl.status_code == 200 and b"Per Diem" in dl.content


@pytest.mark.parametrize("other", ["bob@acme.example", "carol@globex.example"])
async def test_other_users_cannot_retrieve_cite_or_fetch(
    client: httpx.AsyncClient, users: Users, other: str
) -> None:
    allowed = await _ask(client, users, "alice@acme.example")
    finance_citations = [c for c in allowed["citations"] if c["title"] == FINANCE_TITLE]
    assert finance_citations
    target = finance_citations[0]

    # 1. Cannot retrieve: the same question never surfaces the finance document or its text.
    body = await _ask(client, users, other)
    assert all(c["document_id"] != target["document_id"] for c in body["citations"]), body
    assert all("95" not in claim["text"] or "Globex" in claim["text"] for claim in body["claims"])
    for claim in body["claims"]:
        assert "POL-FIN-017" not in claim["text"]

    # 2. Cannot cite: opening the citation id directly returns the same 404 as a nonexistent id.
    hdr = await users.headers(other)
    denied = await client.get(f"/api/citations/{target['citation_id']}", headers=hdr)
    missing = await client.get("/api/citations/chk_" + "0" * 32, headers=hdr)
    assert denied.status_code == 404 and missing.status_code == 404
    assert denied.json() == missing.json()

    # 3. Cannot fetch the document directly.
    dl = await client.get(f"/api/documents/{target['document_id']}/download", headers=hdr)
    assert dl.status_code == 404


async def test_cross_tenant_user_sees_only_own_tenant(client: httpx.AsyncClient, users: Users) -> None:
    body = await _ask(client, users, "carol@globex.example")
    assert body["citations"], body
    assert {c["title"] for c in body["citations"]} == {"Globex Expense Guidelines"}


async def test_unauthenticated_and_forged_requests_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/ask", json={"question": QUESTION})).status_code == 401
    forged = await client.post(
        "/api/ask", json={"question": QUESTION}, headers={"Authorization": "Bearer abc.def.ghi"}
    )
    assert forged.status_code == 401


async def test_denials_are_audited(client: httpx.AsyncClient, users: Users, audit_sink) -> None:  # type: ignore[no-untyped-def]
    await client.get("/api/citations/chk_" + "1" * 32, headers=await users.headers("bob@acme.example"))
    assert any(e["action"] == "citation.open" and e["outcome"] == "denied" for e in audit_sink.events)
    # Audit events never contain document text or tokens.
    blob = str(audit_sink.events)
    assert "Bearer" not in blob and "per diem" not in blob.lower()


async def test_feedback_is_stored_per_user_and_audited(
    client: httpx.AsyncClient, users: Users, core, audit_sink
) -> None:  # type: ignore[no-untyped-def]
    body = await _ask(client, users, "bob@acme.example", "How quickly must a SEV1 page be acknowledged?")
    hdr = await users.headers("bob@acme.example")
    r = await client.post(
        "/api/feedback",
        json={
            "run_id": body["run_id"],
            "rating": "down",
            "reason": "incomplete",
            "comment": "missing escalation detail",
        },
        headers=hdr,
    )
    assert r.status_code == 201
    stored = await core.tables["feedback"].query(
        "11111111-1111-4111-8111-111111111111#b2b2b2b2-0000-4000-8000-000000000002"
    )
    assert any(f["run_id"] == body["run_id"] and f["rating"] == "down" for f in stored)
    assert any(e["action"] == "feedback.submit" for e in audit_sink.events)
    bad = await client.post("/api/feedback", json={"run_id": "run_x", "rating": "meh"}, headers=hdr)
    assert bad.status_code == 422
