"""Hybrid retrieval: filtered BM25 ∥ filtered k-NN → RRF → dedupe → authoritative recheck → rerank → pack."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from erp_auth.models import LABEL_RANK, AuthzContext, DenyReason, PolicySettings, SensitivityLabel, label_rank
from erp_auth.policy import decide, search_filter
from erp_rag.config import RetrievalConfig
from erp_rag.gateway.gateway import ModelGateway, RequestBudget
from erp_rag.gateway.types import Task
from erp_rag.planner import constraint_filters, query_text
from erp_rag.schemas import Candidate, ChunkRecord, EvidencePassage, RetrievalPlan, RetrievalStrategy
from erp_rag.search.opensearch import ChunkIndex
from erp_rag.stores.dynamo import PermissionStore
from erp_rag.text import estimate_tokens, looks_like_injection


@dataclass
class RetrievalStats:
    bm25_hits: int = 0
    vector_hits: int = 0
    fused: int = 0
    deduped: int = 0
    denied_by_recheck: Counter[str] = field(default_factory=Counter)
    excluded_by_model_policy: int = 0
    reranked: int = 0
    packed: int = 0


def rrf_fuse(ranked_lists: list[tuple[str, list[ChunkRecord]]], *, k: int) -> list[Candidate]:
    """Reciprocal Rank Fusion. Each list is ("bm25:i"|"vector:i", ranked chunks)."""
    by_id: dict[str, Candidate] = {}
    for leg, chunks in ranked_lists:
        kind, _, sq = leg.partition(":")
        for rank, chunk in enumerate(chunks, start=1):
            cand = by_id.get(chunk.chunk_id)
            if cand is None:
                cand = by_id[chunk.chunk_id] = Candidate(chunk=chunk)
            cand.fused_score += 1.0 / (k + rank)
            if kind == "bm25":
                cand.bm25_rank = rank if cand.bm25_rank is None else min(cand.bm25_rank, rank)
            else:
                cand.vector_rank = rank if cand.vector_rank is None else min(cand.vector_rank, rank)
            if sq.isdigit() and int(sq) not in cand.matched_subqueries:
                cand.matched_subqueries.append(int(sq))
    return sorted(by_id.values(), key=lambda c: (-c.fused_score, c.chunk.chunk_id))


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Drop exact duplicates (same chunk) and identical content within the same document version."""
    seen: set[tuple[str, str]] = set()
    out: list[Candidate] = []
    for cand in candidates:
        key = (cand.chunk.document_id + cand.chunk.document_version, cand.chunk.content_checksum)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


class HybridRetriever:
    def __init__(
        self, index: ChunkIndex, store: PermissionStore, gateway: ModelGateway, policy: PolicySettings
    ) -> None:
        self.index = index
        self.store = store
        self.gateway = gateway
        self.policy = policy

    async def search(
        self,
        ctx: AuthzContext,
        plan: RetrievalPlan,
        cfg: RetrievalConfig,
        budget: RequestBudget,
        stats: RetrievalStats,
    ) -> list[Candidate]:
        auth = search_filter(ctx)  # derived from server-side context only
        extra = constraint_filters(plan)
        queries = list(plan.subqueries) or [plan.standalone_question]
        if plan.original_question not in queries:
            queries.append(plan.original_question)  # keep the original alongside rewrites
        queries = queries[: cfg.max_subqueries + 1]

        embeddings = await self.gateway.embed(
            [query_text(q, plan) for q in queries],
            purpose="query",
            classification=SensitivityLabel.INTERNAL,
            budget=budget,
        )
        bm25_k = cfg.bm25_top_k * (2 if plan.strategy == RetrievalStrategy.LEXICAL_FIRST else 1)
        vec_k = cfg.vector_top_k * (2 if plan.strategy == RetrievalStrategy.SEMANTIC_FIRST else 1)
        jobs = []
        legs: list[str] = []
        for i, (q, vec) in enumerate(zip(queries, embeddings.value, strict=True)):
            jobs.append(
                self.index.bm25(
                    query_text(q, plan),
                    auth_filter=auth,
                    extra_filters=extra,
                    identifiers=plan.exact_identifiers,
                    k=bm25_k,
                )
            )
            legs.append(f"bm25:{i}")
            jobs.append(self.index.knn(vec, auth_filter=auth, extra_filters=extra, k=vec_k))
            legs.append(f"vector:{i}")
        results = await asyncio.gather(*jobs)
        stats.bm25_hits += sum(len(r) for leg, r in zip(legs, results, strict=True) if leg.startswith("bm25"))
        stats.vector_hits += sum(
            len(r) for leg, r in zip(legs, results, strict=True) if leg.startswith("vector")
        )
        fused = rrf_fuse(list(zip(legs, results, strict=True)), k=cfg.rrf_k)
        stats.fused = len(fused)
        unique = dedupe(fused)
        stats.deduped = len(fused) - len(unique)
        return unique

    async def recheck(
        self, ctx: AuthzContext, candidates: list[Candidate], stats: RetrievalStats
    ) -> list[Candidate]:
        """Authoritative recheck against DynamoDB before any text reaches a model."""
        if not candidates:
            return []
        records = await self.store.get_many(ctx.tenant_id, [c.chunk.document_id for c in candidates])
        allowed: list[Candidate] = []
        for cand in candidates:
            record = records.get(cand.chunk.document_id)
            decision = decide(ctx, record, settings=self.policy, version=cand.chunk.document_version)
            if decision.allowed and record is not None:
                # The index copy of the label may be stale; enforce the authoritative label from here on.
                cand.chunk = cand.chunk.model_copy(update={"sensitivity_label": record.sensitivity_label})
                allowed.append(cand)
            else:
                stats.denied_by_recheck[str(decision.reason or DenyReason.RECORD_MISSING)] += 1
        return allowed

    def model_admissible(self, candidates: list[Candidate], stats: RetrievalStats) -> list[Candidate]:
        """Drop evidence whose label exceeds every approved rerank/generate model (data classification)."""
        ceiling = min(self._task_ceiling(Task.RERANK), self._task_ceiling(Task.GENERATE))
        kept = [c for c in candidates if (label_rank(c.chunk.sensitivity_label) or 99) <= ceiling]
        stats.excluded_by_model_policy += len(candidates) - len(kept)
        return kept

    def _task_ceiling(self, task: Task) -> int:
        route = self.gateway.registry.routes.get(task)
        if route is None:
            return -1
        ranks = [
            LABEL_RANK[self.gateway.registry.models[k].max_classification]
            for k in route.chain
            if self.gateway.registry.models[k].approved
        ]
        return max(ranks, default=-1)

    async def rerank(
        self,
        query: str,
        candidates: list[Candidate],
        cfg: RetrievalConfig,
        budget: RequestBudget,
        stats: RetrievalStats,
    ) -> list[Candidate]:
        pool = candidates[: cfg.rerank_candidate_limit]
        if not pool:
            return []
        texts = [f"{c.chunk.title} | {c.chunk.section}\n{c.chunk.content}" for c in pool]
        result = await self.gateway.rerank(query, texts, classification=max_label(pool), budget=budget)
        for cand, score in zip(pool, result.value, strict=True):
            cand.rerank_score = score
        stats.reranked = len(pool)
        return sorted(pool, key=lambda c: (-(c.rerank_score or 0.0), -c.fused_score, c.chunk.chunk_id))

    async def expand_context(
        self, ctx: AuthzContext, selected: list[Candidate], cfg: RetrievalConfig
    ) -> dict[str, str]:
        """Neighbor/parent expansion for tables and short chunks — authorization filter + same version."""
        if cfg.neighbor_window == 0:
            return {}
        wanted: dict[str, list[str]] = {}
        for cand in selected:
            ch = cand.chunk
            if ch.chunk_type == "table" or len(ch.content) < 300:
                ids = [i for i in (ch.prev_chunk_id, ch.next_chunk_id) if i]
                if ids:
                    wanted[ch.chunk_id] = ids
        if not wanted:
            return {}
        neighbors = await self.index.get_chunks(
            sorted({i for ids in wanted.values() for i in ids}), auth_filter=search_filter(ctx)
        )
        by_id = {n.chunk_id: n for n in neighbors}
        expanded: dict[str, str] = {}
        for cand in selected:
            ch = cand.chunk
            parts: list[tuple[int, str]] = [(ch.ordinal, ch.content)]
            for nid in wanted.get(ch.chunk_id, []):
                n = by_id.get(nid)
                # Same document *version* and same section/table only; a neighbor never widens access
                # because it belongs to a document that already passed the authoritative recheck.
                if (
                    n
                    and n.document_id == ch.document_id
                    and n.document_version == ch.document_version
                    and (n.parent_section_id == ch.parent_section_id)
                ):
                    parts.append((n.ordinal, n.content))
            if len(parts) > 1:
                expanded[ch.chunk_id] = "\n".join(text for _, text in sorted(parts))
        return expanded


def max_label(candidates: list[Candidate]) -> SensitivityLabel:
    return max_of_labels([c.chunk.sensitivity_label for c in candidates])


def max_of_labels(labels: list[str]) -> SensitivityLabel:
    """Highest label present; unknown labels count as restricted."""
    best = SensitivityLabel.PUBLIC
    for label in labels:
        rank = label_rank(label)
        if rank is None:
            return SensitivityLabel.RESTRICTED
        if rank > LABEL_RANK[best]:
            best = SensitivityLabel(label)
    return best


def pack(
    candidates: list[Candidate], cfg: RetrievalConfig, *, expanded: dict[str, str] | None = None
) -> list[EvidencePassage]:
    """Token-budgeted packing with per-document caps (diversity) and series-based revision flags."""
    expanded = expanded or {}
    per_doc: Counter[str] = Counter()
    budget = cfg.context_token_budget
    chosen: list[Candidate] = []
    texts: dict[str, str] = {}
    for cand in candidates:
        if (cand.rerank_score or 0.0) < cfg.min_rerank_score:
            continue
        ch = cand.chunk
        if per_doc[ch.document_id] >= cfg.max_passages_per_document:
            continue
        text = expanded.get(ch.chunk_id, ch.content)
        if ch.chunk_type == "table" and ch.table_header and ch.table_header not in text:
            text = ch.table_header + "\n" + text  # keep table headers with every table fragment
        cost = estimate_tokens(text) + 30
        if cost > budget:
            continue
        budget -= cost
        per_doc[ch.document_id] += 1
        chosen.append(cand)
        texts[ch.chunk_id] = text
        if len(chosen) >= cfg.final_passage_limit:
            break

    latest_by_series: dict[str, tuple[str, str]] = {}
    for cand in chosen:
        ch = cand.chunk
        if ch.series_id and ch.effective_date:
            current = latest_by_series.get(ch.series_id)
            stamp = ch.effective_date.isoformat()
            if current is None or stamp > current[0]:
                latest_by_series[ch.series_id] = (stamp, ch.document_id)

    passages = []
    for i, cand in enumerate(chosen, start=1):
        ch = cand.chunk
        latest = latest_by_series.get(ch.series_id or "")
        passages.append(
            EvidencePassage(
                evidence_id=f"E{i}",
                chunk_id=ch.chunk_id,
                document_id=ch.document_id,
                document_version=ch.document_version,
                title=ch.title,
                location=ch.location,
                section=ch.section,
                text=texts[ch.chunk_id],
                chunk_type=ch.chunk_type,
                rerank_score=float(cand.rerank_score or 0.0),
                sensitivity_label=ch.sensitivity_label,
                source_modified_at=ch.source_modified_at,
                suspected_injection=looks_like_injection(texts[ch.chunk_id]),
                is_latest_revision=latest is None or latest[1] == ch.document_id,
                series_id=ch.series_id,
                effective_date=ch.effective_date,
            )
        )
    return passages


@dataclass(frozen=True)
class Sufficiency:
    sufficient: bool
    limited: bool
    reasons: tuple[str, ...]


def assess_sufficiency(
    passages: list[EvidencePassage], plan: RetrievalPlan, cfg: RetrievalConfig
) -> Sufficiency:
    reasons: list[str] = []
    if len(passages) < cfg.min_supporting_passages:
        return Sufficiency(False, False, ("no_passages_above_threshold",))
    req = plan.evidence_requirements
    if len({p.document_id for p in passages}) < req.min_distinct_documents:
        reasons.append("fewer_distinct_documents_than_required")
    if req.needs_table and not any(p.chunk_type == "table" for p in passages):
        reasons.append("table_evidence_missing")
    return Sufficiency(True, bool(reasons), tuple(reasons))
