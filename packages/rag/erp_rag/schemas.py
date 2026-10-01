"""Typed contracts for chunks, retrieval, generation, citations and judging."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EXTRACTION_VERSION = "extract-v1"


class PublicationStatus(StrEnum):
    STAGED = "staged"
    PUBLISHED = "published"
    RETIRED = "retired"


class ChunkRecord(BaseModel):
    """A searchable chunk. Every field required by the specification is present and typed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    document_id: str
    document_version: str
    chunk_id: str
    source_uri: str
    title: str
    page: int | None = None
    page_end: int | None = None
    location: str  # human-readable source location, e.g. "p.3" or "§2.1" or "line 40-58"
    section: str  # heading path, e.g. "Policy > Leave > Carry-over"
    content: str
    content_checksum: str
    source_modified_at: datetime | None
    extraction_version: str
    embedding_version: str
    allowed_principals: list[str]
    denied_principals: list[str]
    project_ids: list[str]
    project_restricted: bool
    sensitivity_label: str
    acl_version: int
    governance_version: str
    publication_status: PublicationStatus
    ordinal: int
    chunk_type: Literal["text", "table"] = "text"
    table_header: str | None = None
    identifiers: list[str] = []
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    parent_section_id: str | None = None
    # Document series: different documents that are revisions of the same policy (e.g. 2023 vs 2024
    # handbook). Used to flag outdated sources; not used for authorization.
    series_id: str | None = None
    effective_date: datetime | None = None


REQUIRED_CHUNK_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "document_id",
    "document_version",
    "chunk_id",
    "source_uri",
    "location",
    "section",
    "content_checksum",
    "source_modified_at",
    "extraction_version",
    "embedding_version",
    "allowed_principals",
    "denied_principals",
    "sensitivity_label",
    "acl_version",
    "governance_version",
    "publication_status",
)


class RetrievalStrategy(StrEnum):
    HYBRID = "hybrid"
    LEXICAL_FIRST = "lexical_first"  # exact identifiers present
    SEMANTIC_FIRST = "semantic_first"


class MetadataConstraints(BaseModel):
    """Optional narrowing constraints suggested by the planner. Validated in code; can only narrow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_ids: list[str] = Field(default_factory=list, max_length=20)
    sections_contains: str | None = Field(default=None, max_length=200)
    modified_after: datetime | None = None
    chunk_type: Literal["text", "table"] | None = None


class EvidenceRequirements(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_distinct_documents: int = Field(1, ge=1, le=5)
    needs_table: bool = False
    needs_latest_revision: bool = True


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    original_question: str
    standalone_question: str
    acronym_expansions: dict[str, str] = {}
    exact_identifiers: list[str] = Field(default_factory=list, max_length=20)
    suggested_constraints: MetadataConstraints = MetadataConstraints()
    subqueries: list[str] = Field(default_factory=list, max_length=8)
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID
    evidence_requirements: EvidenceRequirements = EvidenceRequirements()
    planner_version: str = "planner-v1"


class Candidate(BaseModel):
    """A retrieved chunk plus scores. Text is only populated after authorization recheck passes."""

    model_config = ConfigDict(extra="forbid")

    chunk: ChunkRecord
    bm25_rank: int | None = None
    vector_rank: int | None = None
    fused_score: float = 0.0
    rerank_score: float | None = None
    matched_subqueries: list[int] = []


class EvidencePassage(BaseModel):
    """A packed, authorized passage handed to the generator/judge. ``evidence_id`` is a short alias."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    chunk_id: str
    document_id: str
    document_version: str
    title: str
    location: str
    section: str
    text: str
    chunk_type: Literal["text", "table"]
    rerank_score: float
    sensitivity_label: str  # authoritative label (from the DynamoDB recheck), drives model routing
    source_modified_at: datetime | None
    suspected_injection: bool = False
    is_latest_revision: bool = True
    series_id: str | None = None
    effective_date: datetime | None = None


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    LIMITED = "limited"  # evidence-limited / partial answer
    ABSTAINED = "abstained"


class GeneratedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class GeneratorOutput(BaseModel):
    """Structured output the generator must produce. Anything else fails validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["answered", "limited", "abstain"]
    claims: list[GeneratedClaim] = Field(default_factory=list, max_length=20)
    conflicts: list[str] = Field(default_factory=list, max_length=10)
    abstain_reason: str | None = None


class Citation(BaseModel):
    """Server-resolved citation. Nothing here comes from model output except the evidence binding."""

    model_config = ConfigDict(frozen=True)

    citation_id: str  # == chunk_id
    evidence_id: str
    document_id: str
    document_version: str
    title: str
    location: str
    section: str
    source_uri: str
    open_url: str
    source_modified_at: datetime | None
    acl_synced_at: datetime | None
    is_latest_revision: bool


class AnswerClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citation_ids: list[str]


class ClaimFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_index: int = Field(ge=0)
    supported: Literal["supported", "partial", "unsupported"]
    citation_mismatch: bool = False
    note: str = Field(default="", max_length=500)


class JudgeVerdict(BaseModel):
    """Schema-validated judge output (rubric judge_v1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric_version: str
    claim_findings: list[ClaimFinding]
    unsupported_claims: list[int] = []
    citation_mismatches: list[int] = []
    relevance: Literal["relevant", "partially_relevant", "irrelevant"]
    contradictions: list[str] = []
    recommendation: Literal["pass", "revise", "abstain"]
    calibrated: bool = False  # never True until calibrated against human-reviewed labels


class ResponseMode(StrEnum):
    STANDARD = "standard"
    HIGH_ASSURANCE = "high_assurance"


class AnswerResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    trace_id: str
    conversation_id: str
    status: AnswerStatus
    mode: ResponseMode
    claims: list[AnswerClaim]
    citations: list[Citation]
    conflicts: list[str] = []
    outdated_sources: list[str] = []
    abstain_reason: str | None = None
    judge: JudgeVerdict | None = None
    judge_status: Literal["passed", "revised", "skipped", "sampled_async", "failed", "not_run"] = "not_run"
    from_cache: bool = False
    config_versions: dict[str, str] = {}
    inference_mode: Literal["fixture", "live"] = "fixture"
