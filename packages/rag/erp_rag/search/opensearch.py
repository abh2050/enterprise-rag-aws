"""OpenSearch chunk index: BM25 and Lucene-HNSW k-NN, both with the mandatory authorization filter.

Engine choice verified 2026-09-30: Lucene engine "offers efficient filtering capabilities"; NMSLIB is
deprecated (OpenSearch docs, knn-methods-engines). Pinned engine version: OpenSearch 3.1.
The index is *derived* state; authorization is rechecked against DynamoDB after every search.
"""

from __future__ import annotations

from typing import Any

from opensearchpy import AsyncOpenSearch, NotFoundError

from erp_rag.schemas import ChunkRecord, PublicationStatus

_SOURCE_EXCLUDES = ["embedding"]


def index_name(embedding_version: str, namespace: str = "erp") -> str:
    safe = "".join(
        c if c.isalnum() or c == "-" else "-" for c in f"{namespace}-chunks-{embedding_version}".lower()
    )
    return safe


def _mapping(dimension: int) -> dict[str, Any]:
    kw = {"type": "keyword"}
    return {
        "settings": {
            "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"},
            "analysis": {
                "analyzer": {
                    "content_en": {"type": "standard", "stopwords": "_english_"},
                },
                "normalizer": {"lower": {"type": "custom", "filter": ["lowercase"]}},
            },
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "tenant_id": kw,
                "document_id": kw,
                "document_version": kw,
                "chunk_id": kw,
                "source_uri": kw,
                "title": {"type": "text", "analyzer": "content_en", "fields": {"raw": kw}},
                "page": {"type": "integer"},
                "page_end": {"type": "integer"},
                "location": kw,
                "section": {"type": "text", "analyzer": "content_en", "fields": {"raw": kw}},
                "content": {"type": "text", "analyzer": "content_en"},
                "content_checksum": kw,
                "source_modified_at": {"type": "date"},
                "extraction_version": kw,
                "embedding_version": kw,
                "allowed_principals": kw,
                "denied_principals": kw,
                "project_ids": kw,
                "project_restricted": {"type": "boolean"},
                "sensitivity_label": kw,
                "acl_version": {"type": "long"},
                "governance_version": kw,
                "publication_status": kw,
                "ordinal": {"type": "integer"},
                "chunk_type": kw,
                "table_header": {"type": "text", "analyzer": "content_en"},
                "identifiers": {"type": "keyword", "normalizer": "lower"},
                "prev_chunk_id": kw,
                "next_chunk_id": kw,
                "parent_section_id": kw,
                "series_id": kw,
                "effective_date": {"type": "date"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"m": 16, "ef_construction": 128},
                    },
                },
            },
        },
    }


class ChunkIndex:
    def __init__(
        self, client: AsyncOpenSearch, *, embedding_version: str, dimension: int, namespace: str = "erp"
    ) -> None:
        self.client = client
        self.embedding_version = embedding_version
        self.dimension = dimension
        self.name = index_name(embedding_version, namespace)

    async def ensure(self) -> None:
        if not await self.client.indices.exists(index=self.name):
            await self.client.indices.create(index=self.name, body=_mapping(self.dimension))
            return
        # Refuse to serve an index whose vector dimension differs from the configured embedding model.
        mapping = await self.client.indices.get_mapping(index=self.name)
        dim = mapping[self.name]["mappings"]["properties"]["embedding"]["dimension"]
        if int(dim) != self.dimension:
            raise RuntimeError(
                f"index {self.name} has dimension {dim}, embedding model expects {self.dimension}; "
                "reindex required"
            )

    async def drop(self) -> None:
        if await self.client.indices.exists(index=self.name):
            await self.client.indices.delete(index=self.name)

    async def refresh(self) -> None:
        await self.client.indices.refresh(index=self.name)

    async def index_chunks(
        self, chunks: list[ChunkRecord], vectors: list[list[float]], *, refresh: bool = True
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks/vectors length mismatch")
        if not chunks:
            return
        body: list[dict[str, Any]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            if chunk.embedding_version != self.embedding_version:
                raise ValueError("chunk embedding_version does not match index")
            if len(vector) != self.dimension:
                raise ValueError("vector dimension mismatch")
            body.append({"index": {"_index": self.name, "_id": chunk.chunk_id}})
            body.append({**chunk.model_dump(mode="json"), "embedding": vector})
        resp = await self.client.bulk(body=body, refresh="wait_for" if refresh else "false")
        if resp.get("errors"):
            failed = [i for i in resp["items"] if i.get("index", {}).get("error")]
            raise RuntimeError(f"bulk index failed for {len(failed)} chunks")

    async def bm25(
        self,
        query: str,
        *,
        auth_filter: dict[str, Any],
        extra_filters: list[dict[str, Any]],
        identifiers: list[str],
        k: int,
    ) -> list[ChunkRecord]:
        should: list[dict[str, Any]] = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["content", "section^0.5", "title^0.5", "table_header^0.5"],
                }
            }
        ]
        if identifiers:
            should.append({"terms": {"identifiers": [i.lower() for i in identifiers], "boost": 5.0}})
        body = {
            "size": k,
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "query": {
                "bool": {
                    "filter": [auth_filter, *extra_filters],
                    "should": should,
                    "minimum_should_match": 1,
                }
            },
        }
        resp = await self.client.search(index=self.name, body=body)
        return [ChunkRecord.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def knn(
        self, vector: list[float], *, auth_filter: dict[str, Any], extra_filters: list[dict[str, Any]], k: int
    ) -> list[ChunkRecord]:
        combined = {"bool": {"filter": [auth_filter, *extra_filters]}}
        body = {
            "size": k,
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "query": {"knn": {"embedding": {"vector": vector, "k": k, "filter": combined}}},
        }
        resp = await self.client.search(index=self.name, body=body)
        return [ChunkRecord.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        try:
            resp = await self.client.get(index=self.name, id=chunk_id, _source_excludes=_SOURCE_EXCLUDES)
        except NotFoundError:
            return None
        return ChunkRecord.model_validate(resp["_source"])

    async def get_chunks(self, chunk_ids: list[str], *, auth_filter: dict[str, Any]) -> list[ChunkRecord]:
        """Fetch specific chunks (neighbors/parents) — still filtered by authorization."""
        if not chunk_ids:
            return []
        body = {
            "size": len(chunk_ids),
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "query": {"bool": {"filter": [auth_filter, {"ids": {"values": chunk_ids}}]}},
        }
        resp = await self.client.search(index=self.name, body=body)
        return [ChunkRecord.model_validate(h["_source"]) for h in resp["hits"]["hits"]]

    async def set_version_status(self, document_id: str, version: str, status: PublicationStatus) -> int:
        resp = await self.client.update_by_query(
            index=self.name,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_id": document_id}},
                            {"term": {"document_version": version}},
                        ]
                    }
                },
                "script": {
                    "source": "ctx._source.publication_status = params.s",
                    "lang": "painless",
                    "params": {"s": status.value},
                },
            },
            refresh=True,
            conflicts="proceed",
        )
        return int(resp.get("updated", 0))

    async def retire_other_versions(self, document_id: str, keep_version: str) -> int:
        resp = await self.client.delete_by_query(
            index=self.name,
            body={
                "query": {
                    "bool": {
                        "filter": [{"term": {"document_id": document_id}}],
                        "must_not": [{"term": {"document_version": keep_version}}],
                    }
                }
            },
            refresh=True,
            conflicts="proceed",
        )
        return int(resp.get("deleted", 0))

    async def update_acl(self, document_id: str, fields: dict[str, Any]) -> int:
        """Propagate permission changes to the index copy (eventually consistent; off the security path)."""
        resp = await self.client.update_by_query(
            index=self.name,
            body={
                "query": {"term": {"document_id": document_id}},
                "script": {
                    "source": "for (e in params.f.entrySet()) { ctx._source[e.getKey()] = e.getValue(); }",
                    "lang": "painless",
                    "params": {"f": fields},
                },
            },
            refresh=True,
            conflicts="proceed",
        )
        return int(resp.get("updated", 0))

    async def delete_document(self, document_id: str) -> int:
        resp = await self.client.delete_by_query(
            index=self.name,
            body={"query": {"term": {"document_id": document_id}}},
            refresh=True,
            conflicts="proceed",
        )
        return int(resp.get("deleted", 0))

    async def count(self, query: dict[str, Any] | None = None) -> int:
        resp = await self.client.count(index=self.name, body={"query": query or {"match_all": {}}})
        return int(resp["count"])


def build_client(url: str, *, auth: str, region: str, verify_certs: bool) -> AsyncOpenSearch:
    kwargs: dict[str, Any] = {"hosts": [url], "verify_certs": verify_certs, "timeout": 10, "max_retries": 2}
    if auth == "sigv4":
        import boto3
        from opensearchpy import AsyncHttpConnection, AWSV4SignerAsyncAuth

        credentials = boto3.Session().get_credentials()
        kwargs.update(
            http_auth=AWSV4SignerAsyncAuth(credentials, region, "es"),
            connection_class=AsyncHttpConnection,
            use_ssl=True,
        )
    return AsyncOpenSearch(**kwargs)
