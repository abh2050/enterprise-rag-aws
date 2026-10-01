"""Build the ingestion pipeline from core services and configured connectors."""

from __future__ import annotations

from pathlib import Path

import httpx

from erp_connectors.artifacts import ArtifactStore, LocalArtifactStore, S3ArtifactStore
from erp_connectors.base import GovernanceAdapter, SourceConnector
from erp_connectors.localfs import LocalFileConnector, ManualGovernanceAdapter
from erp_connectors.scanning import ClamdScanner, DevSignatureScanner, GuardDutyS3TagScanner, MalwareScanner
from erp_ingestion.pipeline import IngestionPipeline
from erp_rag.runtime import Core


def build_artifacts(core: Core) -> ArtifactStore:
    s = core.settings
    if s.artifact_store == "s3":
        import boto3

        if not s.artifact_bucket:
            raise RuntimeError("ERP_ARTIFACT_BUCKET is required for the s3 artifact store")
        return S3ArtifactStore(boto3.client("s3", region_name=s.aws_region), s.artifact_bucket)
    return LocalArtifactStore(s.artifact_root)


def build_scanner(core: Core) -> MalwareScanner:
    s = core.settings
    if s.scanner == "clamav":
        return ClamdScanner(s.clamd_host, s.clamd_port)
    if s.scanner == "guardduty":
        import boto3

        if not s.artifact_bucket:
            raise RuntimeError("GuardDuty scanning requires the S3 artifact bucket")
        return GuardDutyS3TagScanner(boto3.client("s3", region_name=s.aws_region), s.artifact_bucket)
    return DevSignatureScanner()


def build_pipeline(
    core: Core, *, local_roots: dict[str, Path] | None = None, http: httpx.AsyncClient | None = None
) -> IngestionPipeline:
    connectors: dict[str, SourceConnector] = {}
    governance: dict[str, GovernanceAdapter] = {}
    for name, root in (local_roots or {}).items():
        conn = LocalFileConnector(root, name=name)
        connectors[name] = conn
        governance[name] = ManualGovernanceAdapter(conn)
    _add_cloud_sources(core, connectors, governance, http)
    ocr = None
    if core.settings.textract_enabled:
        from erp_connectors.textract import TextractOcr

        ocr = TextractOcr(region=core.settings.aws_region).ocr_page
    return IngestionPipeline(
        connectors=connectors,
        governance=governance,
        artifacts=build_artifacts(core),
        scanner=build_scanner(core),
        gateway=core.gateway,
        index=core.index,
        store=core.store,
        events=core.tables["events"],
        checkpoints=core.tables["workflow"],
        manifests=core.tables["chunk_manifest"],
        connector_state=core.tables["connector_state"],
        audit=core.audit,
        max_file_bytes=core.settings.max_file_bytes,
        ocr=ocr,
    )


def _add_cloud_sources(
    core: Core,
    connectors: dict[str, SourceConnector],
    governance: dict[str, GovernanceAdapter],
    http: httpx.AsyncClient | None,
) -> None:
    """S3 and SharePoint sources from settings (implemented-not-live-tested)."""
    s = core.settings
    if s.s3_sources:
        import boto3

        from erp_connectors.s3 import S3Connector, S3ManifestGovernanceAdapter

        client = boto3.client("s3", region_name=s.aws_region)
        for spec in s.s3_sources:
            bucket, _, prefix = spec.partition("/")
            conn = S3Connector(client, bucket, prefix=prefix)
            connectors[conn.name] = conn
            governance[conn.name] = S3ManifestGovernanceAdapter(conn)
            if s.s3_governance == "purview_datamap":
                governance[conn.name] = _purview_s3(core, http, bucket)
    if s.sharepoint_drives:
        from erp_auth.graph import ClientCredentialTokens, secrets_manager_loader
        from erp_connectors.purview import GovernanceMapping, GraphSensitivityLabelAdapter
        from erp_connectors.sharepoint import SharePointConnector

        if http is None or not s.graph_client_id or not s.graph_client_secret_arn:
            raise RuntimeError("SharePoint sources require Graph app credentials and an HTTP client")
        tokens = ClientCredentialTokens(
            http, s.graph_client_id, secrets_manager_loader(s.graph_client_secret_arn, s.aws_region)
        )
        mapping = GovernanceMapping.load(s.governance_mapping_path)
        for spec in s.sharepoint_drives:
            tenant, _, drive = spec.partition(":")
            sp = SharePointConnector(http, tokens, tenant_id=tenant, drive_id=drive)
            connectors[sp.name] = sp
            governance[sp.name] = GraphSensitivityLabelAdapter(
                http, tokens, mapping, tenant_id=tenant, drive_id=drive
            )


def _purview_s3(core: Core, http: httpx.AsyncClient | None, bucket: str) -> GovernanceAdapter:
    from erp_auth.graph import ClientCredentialTokens, secrets_manager_loader
    from erp_connectors.purview import GovernanceMapping, PurviewDataMapGovernance

    s = core.settings
    if not (
        http
        and s.purview_endpoint
        and s.purview_s3_type_name
        and s.graph_client_id
        and s.graph_client_secret_arn
        and s.entra_allowed_tenants
    ):
        raise RuntimeError("Purview Data Map governance requires endpoint, type name and app credentials")
    tokens = ClientCredentialTokens(
        http,
        s.graph_client_id,
        secrets_manager_loader(s.graph_client_secret_arn, s.aws_region),
        scope="https://purview.azure.net/.default",
    )
    return PurviewDataMapGovernance(
        http,
        tokens,
        GovernanceMapping.load(s.governance_mapping_path),
        tenant_id=s.entra_allowed_tenants[0],
        endpoint=s.purview_endpoint,
        bucket=bucket,
        type_name=s.purview_s3_type_name,
    )
