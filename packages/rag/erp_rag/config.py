"""Runtime settings and environment safety checks.

Every non-local environment (dev/staging/prod on AWS) must use real providers. Startup fails if any
development-only component is enabled there, so a misconfiguration cannot silently ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

Environment = Literal["local", "test", "dev", "staging", "prod"]
LOCAL_ENVIRONMENTS: frozenset[str] = frozenset({"local", "test"})


class UnsafeConfigurationError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERP_", env_file=None, extra="ignore")

    environment: Environment = "local"
    # Which process this is. Only the API serves user requests, so only it requires an identity provider.
    service_role: Literal["api", "worker", "cli"] = "api"

    # Identity
    auth_provider: Literal["dev", "entra"] = "dev"
    api_audience: str = "api://erp-local"
    dev_idp_issuer: str = "http://localhost:8000/dev-idp"
    dev_idp_key_path: Path | None = None
    dev_directory_path: Path = REPO_ROOT / "config" / "dev-directory.yaml"
    entitlements_path: Path = REPO_ROOT / "config" / "entitlements.yaml"
    entra_api_client_id: str | None = None
    entra_allowed_tenants: list[str] = []
    entra_required_scopes: list[str] = ["access_as_user"]
    entra_accepted_app_roles: list[str] = []
    graph_client_id: str | None = None
    graph_client_secret_arn: str | None = None

    # Models
    model_provider: Literal["fixture", "bedrock"] = "fixture"
    model_registry_path: Path | None = None
    aws_region: str = "us-east-2"
    # Regions the Bedrock provider may call (deployment/data-residency allowlist).
    bedrock_allowed_regions: list[str] = ["us-east-2"]

    # Retrieval / search
    retrieval_config_path: Path = REPO_ROOT / "config" / "retrieval.v1.yaml"
    opensearch_url: str = "http://localhost:9200"
    opensearch_auth: Literal["none", "sigv4"] = "none"
    opensearch_verify_certs: bool = True
    index_namespace: str = Field(default="erp", pattern=r"^[a-z0-9-]{1,40}$")

    # State
    dynamodb_endpoint: str | None = "http://localhost:8001"
    table_prefix: str = "erp-local-"

    # Artifacts / ingestion
    artifact_store: Literal["local", "s3"] = "local"
    artifact_root: Path = REPO_ROOT / "var" / "artifacts"
    # Local connector roots (name -> directory). Each immediate subdirectory is a collection.
    local_sources: dict[str, Path] = {
        "localfs-synthetic": REPO_ROOT / "data" / "synthetic",
        "localfs-private": REPO_ROOT / "data" / "private",
    }
    artifact_bucket: str | None = None
    # S3 sources: "bucket" or "bucket/prefix". Governance: manual (_access.yaml) or Purview Data Map.
    s3_sources: list[str] = []
    s3_governance: Literal["manual", "purview_datamap"] = "manual"
    purview_endpoint: str | None = None
    purview_s3_type_name: str | None = None  # Atlas type for S3 objects — verify against a real scan
    governance_mapping_path: Path = REPO_ROOT / "config" / "governance-map.v1.yaml"
    # SharePoint sources: "tenant_id:drive_id" (requires Graph app credentials).
    sharepoint_drives: list[str] = []
    scanner: Literal["dev_signature", "guardduty", "clamav"] = "dev_signature"
    clamd_host: str = "127.0.0.1"
    clamd_port: int = 3310
    # AWS: admin replays are executed by the ingestion worker (which has the scanner) via Step Functions.
    ingestion_state_machine_arn: str | None = None
    textract_enabled: bool = False
    max_file_bytes: int = 50 * 1024 * 1024

    # API
    cors_origins: list[str] = ["http://localhost:5173"]
    public_base_url: str = "http://localhost:8000"

    # Observability
    tracing: Literal["noop", "langsmith"] = "noop"
    langsmith_endpoint: str | None = None
    langsmith_project: str = "enterprise-rag"
    audit_dir: Path = REPO_ROOT / "var" / "audit"
    audit_log_group: str | None = None  # dedicated CloudWatch Logs group for the security audit trail

    # Policy
    max_acl_age_seconds: int = Field(default=24 * 3600, gt=0)
    judge_sample_rate_standard: float = Field(default=0.1, ge=0.0, le=1.0)

    @property
    def is_local(self) -> bool:
        return self.environment in LOCAL_ENVIRONMENTS

    def unsafe_reasons(self) -> list[str]:
        """Reasons this configuration must not run outside local/test."""
        reasons: list[str] = []
        if self.auth_provider == "dev" and self.service_role == "api":
            reasons.append("development identity provider enabled")
        if self.model_provider == "fixture":
            reasons.append("fixture (fake) model provider enabled")
        if self.scanner == "dev_signature":
            reasons.append("development malware scanner enabled")
        if self.dynamodb_endpoint:
            reasons.append("DynamoDB endpoint override (local DynamoDB) configured")
        if self.opensearch_auth == "none":
            reasons.append("OpenSearch without SigV4 authentication")
        if not self.opensearch_verify_certs:
            reasons.append("OpenSearch TLS verification disabled")
        if not self.opensearch_url.startswith("https://"):
            reasons.append("OpenSearch URL is not HTTPS")
        if self.artifact_store == "local":
            reasons.append("local filesystem artifact store")
        if any(o == "*" for o in self.cors_origins):
            reasons.append("wildcard CORS origin")
        if (
            self.service_role == "api"
            and self.auth_provider == "entra"
            and (not self.entra_api_client_id or not self.entra_allowed_tenants)
        ):
            reasons.append("Entra provider missing client id or tenant allowlist")
        return reasons

    def assert_safe_for_environment(self) -> None:
        if self.is_local:
            return
        reasons = self.unsafe_reasons()
        if reasons:
            raise UnsafeConfigurationError(
                f"refusing to start in environment '{self.environment}': " + "; ".join(reasons)
            )


class RetrievalConfig(BaseModel):
    """Versioned retrieval settings. Defaults are starting points, not validated optima."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: str
    bm25_top_k: int = Field(50, ge=1, le=500)
    vector_top_k: int = Field(50, ge=1, le=500)
    rrf_k: int = Field(60, ge=1)
    rerank_candidate_limit: int = Field(40, ge=1, le=200)
    final_passage_limit: int = Field(10, ge=1, le=50)
    max_retrieval_retries: int = Field(1, ge=0, le=3)
    max_subqueries: int = Field(3, ge=1, le=8)
    max_passages_per_document: int = Field(3, ge=1)
    context_token_budget: int = Field(6000, ge=256)
    min_rerank_score: float = Field(0.15, ge=0.0, le=1.0)
    min_supporting_passages: int = Field(1, ge=1)
    neighbor_window: int = Field(1, ge=0, le=3)
    max_repair_cycles: int = Field(1, ge=0, le=1)
    max_history_turns: int = Field(4, ge=0, le=20)

    @classmethod
    def load(cls, path: Path) -> RetrievalConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))
