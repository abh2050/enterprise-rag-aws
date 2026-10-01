"""Composition root for core services shared by the API and ingestion workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from erp_auth.models import PolicySettings
from erp_observability.audit import AuditLog, FileSink, LoggerSink
from erp_observability.tracing import current_trace_id
from erp_rag.config import REPO_ROOT, RetrievalConfig, Settings
from erp_rag.gateway.gateway import ModelGateway, ModelRegistry
from erp_rag.gateway.types import ModelProvider, RouteRecord
from erp_rag.judge import Rubric
from erp_rag.retrieval import HybridRetriever
from erp_rag.search.opensearch import ChunkIndex, build_client
from erp_rag.service import QAService
from erp_rag.stores.dynamo import Dynamo, JsonTable, PermissionStore


@dataclass
class Core:
    settings: Settings
    cfg: RetrievalConfig
    policy: PolicySettings
    db: Dynamo
    store: PermissionStore
    tables: dict[str, JsonTable]
    gateway: ModelGateway
    index: ChunkIndex
    audit: AuditLog
    qa: QAService
    providers: dict[str, ModelProvider]
    os_client: Any

    async def close(self) -> None:
        await self.qa.drain_background()
        await self.os_client.close()


def registry_path(settings: Settings) -> Path:
    if settings.model_registry_path:
        return settings.model_registry_path
    return REPO_ROOT / "config" / f"models.{settings.model_provider}.yaml"


def build_providers(settings: Settings) -> dict[str, ModelProvider]:
    if settings.model_provider == "fixture":
        from erp_rag.gateway.providers.fixture import FixtureProvider

        return {"fixture": FixtureProvider()}
    from erp_rag.gateway.providers.bedrock import BedrockProvider

    return {
        "bedrock": BedrockProvider(
            region=settings.aws_region, allowed_regions=frozenset(settings.bedrock_allowed_regions)
        )
    }


async def build_core(
    settings: Settings, *, providers: dict[str, ModelProvider] | None = None, audit: AuditLog | None = None
) -> Core:
    settings.assert_safe_for_environment()
    if settings.tracing == "langsmith":
        from erp_observability.langsmith_exporter import build_langsmith_exporter
        from erp_observability.tracing import Tracer, set_tracer

        set_tracer(Tracer(build_langsmith_exporter(settings.langsmith_endpoint, settings.langsmith_project)))
    cfg = RetrievalConfig.load(settings.retrieval_config_path)
    policy = PolicySettings(max_acl_age_seconds=settings.max_acl_age_seconds)
    db = Dynamo(
        prefix=settings.table_prefix, region=settings.aws_region, endpoint_url=settings.dynamodb_endpoint
    )
    if settings.is_local:
        db.ensure_tables()
    store = PermissionStore(db)
    tables = {
        name: JsonTable(db, name)
        for name in (
            "chunk_manifest",
            "workflow",
            "conversations",
            "answer_cache",
            "feedback",
            "usage",
            "events",
            "connector_state",
        )
    }
    registry = ModelRegistry.load(registry_path(settings))
    if registry.inference_mode == "fixture" and not settings.is_local:
        raise RuntimeError("fixture model registry is not allowed outside local/test")
    providers = providers or build_providers(settings)

    usage = tables["usage"]

    async def route_sink(record: RouteRecord) -> None:
        import time

        await usage.put(
            {"trace_id": current_trace_id(), "record_id": f"{time.time_ns()}-{record.task}"},
            record.model_dump(mode="json"),
            ttl=90 * 24 * 3600,
        )

    gateway = ModelGateway(registry=registry, providers=providers, route_sink=route_sink)
    emb = registry.embedding_spec
    assert emb.embedding_version and emb.embedding_dimension
    os_client = build_client(
        settings.opensearch_url,
        auth=settings.opensearch_auth,
        region=settings.aws_region,
        verify_certs=settings.opensearch_verify_certs,
    )
    index = ChunkIndex(
        os_client,
        embedding_version=emb.embedding_version,
        dimension=emb.embedding_dimension,
        namespace=settings.index_namespace,
    )
    await index.ensure()
    if audit is None:
        if settings.audit_log_group:
            from erp_observability.audit import CloudWatchLogsSink

            audit = AuditLog([CloudWatchLogsSink(settings.audit_log_group, region=settings.aws_region)])
        elif settings.is_local:
            audit = AuditLog([FileSink(settings.audit_dir)])
        else:
            audit = AuditLog([LoggerSink()])
    acronyms: dict[str, str] = yaml.safe_load((REPO_ROOT / "config" / "acronyms.yaml").read_text())[
        "acronyms"
    ]
    rubric = Rubric.load(REPO_ROOT / "evals" / "rubrics" / "judge_v1.yaml")
    qa = QAService(
        retriever=HybridRetriever(index, store, gateway, policy),
        gateway=gateway,
        store=store,
        cfg=cfg,
        policy=policy,
        rubric=rubric,
        acronyms=acronyms,
        audit=audit,
        workflow_table=tables["workflow"],
        conversations=tables["conversations"],
        answer_cache=tables["answer_cache"],
        judge_sample_rate=settings.judge_sample_rate_standard,
    )
    return Core(
        settings=settings,
        cfg=cfg,
        policy=policy,
        db=db,
        store=store,
        tables=tables,
        gateway=gateway,
        index=index,
        audit=audit,
        qa=qa,
        providers=providers,
        os_client=os_client,
    )
