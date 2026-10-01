"""Verifies (against real OpenSearch 3.1) that BOTH retrieval legs enforce the authorization filter.

The k-NN case proves *efficient* (pre-)filtering: the nearest neighbours all belong to a denied group;
a post-filter would return nothing, efficient filtering returns the k nearest *allowed* chunks.
"""

from __future__ import annotations

import math
import secrets
from datetime import UTC, datetime

import pytest

from erp_auth.models import AuthzContext, SensitivityLabel
from erp_auth.policy import search_filter
from erp_rag.config import REPO_ROOT
from erp_rag.schemas import REQUIRED_CHUNK_FIELDS, ChunkRecord, PublicationStatus
from erp_rag.search.opensearch import ChunkIndex, build_client
from tests.conftest import require_services

pytestmark = pytest.mark.integration
DIM = 8


def _vec(angle: float) -> list[float]:
    v = [math.cos(angle), math.sin(angle)] + [0.0] * (DIM - 2)
    return v


def _chunk(
    i: int,
    *,
    group: str,
    tenant: str = "t1",
    label: str = "internal",
    text: str = "quarterly revenue report",
    status: PublicationStatus = PublicationStatus.PUBLISHED,
    denied: list[str] | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        tenant_id=tenant,
        document_id=f"doc_{i}",
        document_version="v1",
        chunk_id=f"chk_{i}",
        source_uri="u",
        title=f"Doc {i}",
        location="p. 1",
        section="S",
        content=f"{text} number {i}",
        content_checksum=str(i),
        source_modified_at=datetime(2024, 1, 1, tzinfo=UTC),
        extraction_version="x",
        embedding_version="test-8",
        allowed_principals=[f"group:{group}"],
        denied_principals=denied or [],
        project_ids=[],
        project_restricted=False,
        sensitivity_label=label,
        acl_version=1,
        governance_version="g",
        publication_status=status,
        ordinal=0,
    )


@pytest.fixture
async def index():  # type: ignore[no-untyped-def]
    require_services()
    client = build_client("http://localhost:9200", auth="none", region="us-west-2", verify_certs=True)
    idx = ChunkIndex(
        client, embedding_version="test-8", dimension=DIM, namespace=f"erpfilt-{secrets.token_hex(3)}"
    )
    await idx.ensure()
    yield idx
    await idx.drop()
    await client.close()


CTX = AuthzContext(
    tenant_id="t1",
    user_id="u",
    principals=frozenset({"user:u", "group:allowed"}),
    clearance=SensitivityLabel.INTERNAL,
)


async def test_knn_uses_efficient_prefilter(index: ChunkIndex) -> None:
    denied = [_chunk(i, group="denied") for i in range(30)]
    allowed = [_chunk(100 + i, group="allowed") for i in range(3)]
    # Denied chunks are nearest to the query (angle ~0); allowed ones are far away (angle ~pi/2).
    vectors = [_vec(0.001 * i) for i in range(30)] + [_vec(math.pi / 2 - 0.01 * i) for i in range(3)]
    await index.index_chunks(denied + allowed, vectors)
    hits = await index.knn(_vec(0.0), auth_filter=search_filter(CTX), extra_filters=[], k=3)
    assert {h.chunk_id for h in hits} == {"chk_100", "chk_101", "chk_102"}


async def test_bm25_and_knn_enforce_all_filter_dimensions(index: ChunkIndex) -> None:
    chunks = [
        _chunk(1, group="allowed"),  # visible
        _chunk(2, group="allowed", tenant="t2"),  # other tenant
        _chunk(3, group="other"),  # other group
        _chunk(4, group="allowed", denied=["user:u"]),  # explicit deny
        _chunk(5, group="allowed", label="confidential"),  # above clearance
        _chunk(6, group="allowed", label="unlabeled"),  # unknown label
        _chunk(7, group="allowed", status=PublicationStatus.STAGED),  # not yet published
    ]
    await index.index_chunks(chunks, [_vec(0.01 * i) for i in range(len(chunks))])
    auth = search_filter(CTX)
    bm25 = await index.bm25("quarterly revenue", auth_filter=auth, extra_filters=[], identifiers=[], k=50)
    knn = await index.knn(_vec(0.0), auth_filter=auth, extra_filters=[], k=50)
    assert [h.chunk_id for h in bm25] == ["chk_1"]
    assert [h.chunk_id for h in knn] == ["chk_1"]
    # Neighbour fetch by explicit id is filtered too.
    fetched = await index.get_chunks([c.chunk_id for c in chunks], auth_filter=auth)
    assert [h.chunk_id for h in fetched] == ["chk_1"]


async def test_dimension_mismatch_requires_reindex(index: ChunkIndex) -> None:
    wrong = ChunkIndex(
        index.client, embedding_version="test-8", dimension=DIM * 2, namespace=index.name.split("-chunks-")[0]
    )
    with pytest.raises(RuntimeError, match="reindex required"):
        await wrong.ensure()


async def test_ingested_chunks_carry_required_metadata(core, ingested) -> None:  # type: ignore[no-untyped-def]
    resp = await core.index.client.search(
        index=core.index.name, body={"size": 500, "query": {"match_all": {}}}
    )
    hits = resp["hits"]["hits"]
    assert hits
    for h in hits:
        src = h["_source"]
        for field in REQUIRED_CHUNK_FIELDS:
            assert field in src, field
        assert src["embedding_version"] == core.index.embedding_version
        assert src["document_version"].startswith("v_") and src["chunk_id"].startswith("chk_")
        manifest = await core.tables["chunk_manifest"].get(
            {"document_id": src["document_id"], "document_version": src["document_version"]}
        )
        assert manifest and src["chunk_id"] in manifest["chunk_ids"]  # chunk → source revision mapping
    assert REPO_ROOT.exists()
