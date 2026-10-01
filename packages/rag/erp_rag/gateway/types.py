"""Typed task interfaces for the model gateway."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from erp_auth.models import SensitivityLabel
from erp_rag.schemas import EvidencePassage


class Task(StrEnum):
    PLAN = "plan"
    EMBED = "embed"
    RERANK = "rerank"
    GENERATE = "generate"
    JUDGE = "judge"


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    provider: Literal["fixture", "bedrock"]
    model_id: str
    tasks: list[Task]
    region: str
    residency: Literal["local", "in_region", "geo_us", "global"]
    max_classification: SensitivityLabel
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(default=2048, gt=0)
    modalities: list[Literal["text", "image"]] = ["text"]
    price_input_per_1k: float = Field(default=0.0, ge=0)
    price_output_per_1k: float = Field(default=0.0, ge=0)
    embedding_dimension: int | None = None
    embedding_version: str | None = None
    approved: bool = True
    legacy_risk: bool = False
    notes: str = ""


class TaskRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chain: list[str] = Field(min_length=1)  # primary first, then approved fallbacks in order
    deadline_ms: int = Field(default=20000, gt=0)  # total budget for the task across retries + fallbacks
    attempt_timeout_ms: int = Field(default=10000, gt=0)  # one hung call must not starve the fallbacks
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrency: int = Field(default=8, ge=1)
    backoff_base_ms: int = Field(default=200, ge=0)


class Budgets(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd_per_request: float = Field(default=0.50, gt=0)
    max_tokens_per_request: int = Field(default=200_000, gt=0)


class RouteRecord(BaseModel):
    """Persisted for every gateway call (usage ledger + trace)."""

    task: Task
    model_key: str | None
    model_id: str | None
    provider: str | None
    route_reason: str
    config_version: str
    attempts: int
    latency_ms: float
    usage: Usage
    est_cost_usd: float
    outcome: Literal["ok", "failed"]
    fallback_used: bool
    error_category: str | None = None
    skipped: list[str] = []


class PlanRequest(BaseModel):
    question: str
    history_questions: list[str] = []
    acronyms: dict[str, str] = {}
    max_subqueries: int = 3


class PlanDraft(BaseModel):
    """What a planner model may return. Validated and normalized by application code."""

    model_config = ConfigDict(extra="forbid")

    standalone_question: str = Field(min_length=1, max_length=2000)
    acronym_expansions: dict[str, str] = {}
    exact_identifiers: list[str] = Field(default_factory=list, max_length=20)
    subqueries: list[str] = Field(default_factory=list, max_length=10)
    strategy: Literal["hybrid", "lexical_first", "semantic_first"] = "hybrid"
    min_distinct_documents: int = Field(default=1, ge=1, le=5)
    needs_table: bool = False
    suggested_document_ids: list[str] = Field(default_factory=list, max_length=20)
    suggested_section: str | None = None
    # Anything else (tenant, principals, labels) is rejected by extra="forbid".


class GenerateRequest(BaseModel):
    question: str
    evidence: list[EvidencePassage]
    prompt_version: str
    repair_feedback: str | None = None


class JudgeRequest(BaseModel):
    question: str
    claims: list[dict[str, object]]
    evidence: list[EvidencePassage]
    rubric_version: str
    rubric_text: str


class ModelProvider(Protocol):
    async def plan(self, spec: ModelSpec, req: PlanRequest) -> tuple[str, Usage]: ...

    async def embed(
        self, spec: ModelSpec, texts: list[str], purpose: Literal["query", "document"]
    ) -> tuple[list[list[float]], Usage]: ...

    async def rerank(
        self, spec: ModelSpec, query: str, documents: list[str]
    ) -> tuple[list[float], Usage]: ...

    async def generate(self, spec: ModelSpec, req: GenerateRequest) -> tuple[str, Usage]: ...

    async def judge(self, spec: ModelSpec, req: JudgeRequest) -> tuple[str, Usage]: ...


class ProviderError(Exception):
    """Non-retryable provider failure (e.g. validation, access denied)."""

    category = "provider_error"


class RetryableProviderError(ProviderError):
    category = "retryable"


class ThrottledError(RetryableProviderError):
    category = "throttled"
