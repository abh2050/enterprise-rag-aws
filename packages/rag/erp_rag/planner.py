"""Query optimization: model-proposed draft → application-validated ``RetrievalPlan``.

The planner model can only *suggest*. Code bounds subqueries, keeps the original question, rejects
invented identifiers, and turns suggested constraints into filters that can only narrow results.
Authorization filters are composed separately and are never derived from planner output.
"""

from __future__ import annotations

import re
from typing import Any

from erp_rag.config import RetrievalConfig
from erp_rag.gateway.gateway import GatewayExhaustedError, ModelGateway, NoEligibleModelError, RequestBudget
from erp_rag.gateway.types import PlanDraft, PlanRequest
from erp_rag.schemas import (
    EvidenceRequirements,
    MetadataConstraints,
    RetrievalPlan,
    RetrievalStrategy,
)
from erp_rag.text import extract_identifiers

_DOC_ID_RE = re.compile(r"^doc_[0-9a-f]{32}$")
MAX_QUESTION_CHARS = 2000


def fallback_plan(question: str) -> RetrievalPlan:
    """Deterministic plan used when the planner model is unavailable."""
    ids = extract_identifiers(question)
    return RetrievalPlan(
        original_question=question,
        standalone_question=question,
        exact_identifiers=ids,
        subqueries=[question],
        strategy=RetrievalStrategy.LEXICAL_FIRST if ids else RetrievalStrategy.HYBRID,
        planner_version="planner-fallback-v1",
    )


def validate_draft(
    question: str,
    history_questions: list[str],
    draft: PlanDraft,
    cfg: RetrievalConfig,
    acronyms: dict[str, str],
) -> RetrievalPlan:
    standalone = draft.standalone_question.strip()[:MAX_QUESTION_CHARS] or question
    allowed_text = " ".join([question, *history_questions])

    # Identifiers must literally occur in the user's own text; models may not invent them.
    identifiers = [i for i in draft.exact_identifiers if i and i in allowed_text][:20]
    for found in extract_identifiers(question):
        if found not in identifiers:
            identifiers.append(found)

    # Only expand acronyms that appear in the question and exist in the governed glossary.
    expansions = {
        k: acronyms[k]
        for k in draft.acronym_expansions
        if k in acronyms and re.search(rf"\b{re.escape(k)}\b", question)
    }

    subqueries: list[str] = []
    for raw_sq in [standalone, *draft.subqueries]:
        sq = raw_sq.strip()[:500]
        if sq and sq not in subqueries:
            subqueries.append(sq)
    subqueries = subqueries[: cfg.max_subqueries]

    constraints = MetadataConstraints(
        document_ids=[d for d in draft.suggested_document_ids if _DOC_ID_RE.match(d)][:20],
        sections_contains=draft.suggested_section[:200] if draft.suggested_section else None,
    )
    strategy = RetrievalStrategy(draft.strategy)
    if identifiers and strategy == RetrievalStrategy.SEMANTIC_FIRST:
        strategy = RetrievalStrategy.HYBRID  # exact identifiers must keep the lexical leg prominent

    return RetrievalPlan(
        original_question=question,
        standalone_question=standalone,
        acronym_expansions=expansions,
        exact_identifiers=identifiers,
        suggested_constraints=constraints,
        subqueries=subqueries,
        strategy=strategy,
        evidence_requirements=EvidenceRequirements(
            # A model may only demand multiple sources when it actually decomposed the question; otherwise
            # complete single-source answers would be downgraded to "limited" (observed live with Nova Lite).
            min_distinct_documents=min(draft.min_distinct_documents, max(1, len(subqueries) - 1)),
            needs_table=draft.needs_table,
        ),
    )


async def build_plan(
    gateway: ModelGateway,
    *,
    question: str,
    history_questions: list[str],
    cfg: RetrievalConfig,
    acronyms: dict[str, str],
    budget: RequestBudget,
) -> tuple[RetrievalPlan, str]:
    """Returns the plan and the route outcome ("model" or "fallback:<reason>")."""
    try:
        result = await gateway.plan(
            PlanRequest(
                question=question,
                history_questions=history_questions[-cfg.max_history_turns :],
                acronyms=acronyms,
                max_subqueries=cfg.max_subqueries,
            ),
            budget=budget,
        )
    except (NoEligibleModelError, GatewayExhaustedError) as exc:
        return fallback_plan(question), f"fallback:{type(exc).__name__}"
    return validate_draft(question, history_questions, result.value, cfg, acronyms), "model"


def constraint_filters(plan: RetrievalPlan) -> list[dict[str, Any]]:
    """Narrowing-only filters derived from validated suggestions (ANDed with the auth filter)."""
    filters: list[dict[str, Any]] = []
    c = plan.suggested_constraints
    if c.document_ids:
        filters.append({"terms": {"document_id": c.document_ids}})
    if c.sections_contains:
        filters.append({"match_phrase": {"section": c.sections_contains}})
    if c.modified_after:
        filters.append({"range": {"source_modified_at": {"gte": c.modified_after.isoformat()}}})
    if c.chunk_type:
        filters.append({"term": {"chunk_type": c.chunk_type}})
    return filters


def query_text(subquery: str, plan: RetrievalPlan) -> str:
    """Lexical query text: subquery plus governed acronym expansions."""
    if not plan.acronym_expansions:
        return subquery
    return subquery + " " + " ".join(plan.acronym_expansions.values())
