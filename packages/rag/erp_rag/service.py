"""Question-answering service: orchestrates the deterministic workflow end to end."""

from __future__ import annotations

import asyncio
import hashlib
import random
import secrets
from dataclasses import dataclass, field
from typing import Any

from erp_auth.models import AuthzContext, PolicySettings, SensitivityLabel
from erp_auth.policy import decide
from erp_observability.audit import AuditLog
from erp_observability.tracing import current_trace_id, get_tracer
from erp_rag.config import RetrievalConfig
from erp_rag.gateway.gateway import (
    BudgetExceededError,
    GatewayExhaustedError,
    ModelGateway,
    NoEligibleModelError,
    RequestBudget,
)
from erp_rag.gateway.types import GenerateRequest
from erp_rag.generation import ValidatedAnswer, resolve_citations, validate_output
from erp_rag.judge import JudgeOutcome, Rubric, repair_feedback, run_judge, supported_subset
from erp_rag.planner import MAX_QUESTION_CHARS, build_plan
from erp_rag.prompts import GENERATOR_PROMPT_VERSION
from erp_rag.retrieval import HybridRetriever, RetrievalStats, assess_sufficiency, max_of_labels, pack
from erp_rag.schemas import (
    AnswerResponse,
    AnswerStatus,
    EvidencePassage,
    MetadataConstraints,
    ResponseMode,
    RetrievalPlan,
    RetrievalStrategy,
)
from erp_rag.stores.dynamo import JsonTable, PermissionStore
from erp_rag.workflow import Stage, WorkflowRun

CACHE_TTL_SECONDS = 600
HISTORY_WITHHELD = "[Earlier answer withheld: access to one or more of its sources has changed.]"


class QuestionRejectedError(ValueError):
    pass


@dataclass
class HistoryTurn:
    turn: str
    question: str
    status: str
    answer_text: str
    withheld: bool
    cited: list[list[str]]


@dataclass
class QAService:
    retriever: HybridRetriever
    gateway: ModelGateway
    store: PermissionStore
    cfg: RetrievalConfig
    policy: PolicySettings
    rubric: Rubric
    acronyms: dict[str, str]
    audit: AuditLog
    workflow_table: JsonTable | None
    conversations: JsonTable
    answer_cache: JsonTable
    judge_sample_rate: float = 0.1
    _bg: set[asyncio.Task[None]] = field(default_factory=set)
    _bg_limit: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))

    # ------------------------------------------------------------------ history

    def _conv_key(self, ctx: AuthzContext, conversation_id: str) -> str:
        # Namespaced by tenant + user: a conversation id can never address another user's history.
        return f"{ctx.tenant_id}#{ctx.user_id}#{conversation_id}"

    async def load_history(self, ctx: AuthzContext, conversation_id: str) -> list[HistoryTurn]:
        """Return the user's history with answers redacted if any cited source is no longer authorized."""
        items = await self.conversations.query(self._conv_key(ctx, conversation_id), limit=50)
        doc_ids = sorted({c[0] for it in items for c in it.get("cited", [])})
        records = await self.store.get_many(ctx.tenant_id, doc_ids) if doc_ids else {}
        turns: list[HistoryTurn] = []
        for it in items:
            cited: list[list[str]] = it.get("cited", [])
            ok = all(
                decide(ctx, records.get(doc), settings=self.policy, version=ver).allowed for doc, ver in cited
            )
            turns.append(
                HistoryTurn(
                    turn=str(it["_sk"]),
                    question=it["question"],
                    status=it["status"],
                    answer_text=it["answer_text"] if ok else HISTORY_WITHHELD,
                    withheld=not ok,
                    cited=cited if ok else [],
                )
            )
        return turns

    async def _save_turn(
        self, ctx: AuthzContext, conversation_id: str, question: str, resp: AnswerResponse
    ) -> None:
        await self.conversations.put(
            {"conversation_key": self._conv_key(ctx, conversation_id), "turn": f"{_now_key()}"},
            {
                "question": question,
                "status": resp.status,
                "answer_text": " ".join(c.text for c in resp.claims),
                "cited": sorted({(c.document_id, c.document_version) for c in resp.citations}),
                "run_id": resp.run_id,
            },
        )

    # ------------------------------------------------------------------ cache

    def _cache_key(self, ctx: AuthzContext, question: str, mode: ResponseMode) -> str:
        norm = " ".join(question.lower().split())
        material = "|".join(
            [
                ctx.tenant_id,
                ctx.principal_hash,
                mode,
                norm,
                self.cfg.config_version,
                self.gateway.registry.config_version,
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()

    async def _cache_get(self, ctx: AuthzContext, key: str) -> AnswerResponse | None:
        entry = await self.answer_cache.get({"cache_key": key})
        if not entry:
            return None
        cited: list[list[Any]] = entry["cited"]
        records = await self.store.get_many(ctx.tenant_id, [c[0] for c in cited]) if cited else {}
        for doc, ver, acl_version in cited:
            rec = records.get(doc)
            if (
                rec is None
                or rec.acl_version != acl_version
                or not decide(ctx, rec, settings=self.policy, version=ver).allowed
            ):
                await self.answer_cache.delete({"cache_key": key})
                return None
        return AnswerResponse.model_validate(entry["response"])

    async def _cache_put(self, ctx: AuthzContext, key: str, resp: AnswerResponse) -> None:
        if resp.status == AnswerStatus.ABSTAINED or not resp.citations:
            return
        records = await self.store.get_many(ctx.tenant_id, [c.document_id for c in resp.citations])
        cited = []
        for c in resp.citations:
            rec = records.get(c.document_id)
            if rec is None:
                return
            cited.append([c.document_id, c.document_version, rec.acl_version])
        await self.answer_cache.put(
            {"cache_key": key},
            {"response": resp.model_dump(mode="json"), "cited": cited},
            ttl=CACHE_TTL_SECONDS,
        )

    # ------------------------------------------------------------------ main entry

    async def ask(
        self,
        ctx: AuthzContext,
        question: str,
        *,
        conversation_id: str | None = None,
        mode: ResponseMode = ResponseMode.STANDARD,
    ) -> AnswerResponse:
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise QuestionRejectedError("question must be 1-2000 characters")
        conversation_id = conversation_id or secrets.token_hex(8)
        tracer = get_tracer()
        run = WorkflowRun(self.workflow_table, trace_id=current_trace_id())
        await run.start(mode=str(mode), tenant_id=ctx.tenant_id)
        with tracer.span("qa.ask", run_id=run.run_id, mode=str(mode)) as span:
            history = await self.load_history(ctx, conversation_id)
            history_questions = [t.question for t in history][-self.cfg.max_history_turns :]

            cache_key = self._cache_key(ctx, question, mode) if not history else None
            if cache_key:
                cached = await self._cache_get(ctx, cache_key)
                if cached is not None:
                    await run.advance(Stage.RELEASED, cache="hit")
                    resp = cached.model_copy(
                        update={
                            "run_id": run.run_id,
                            "trace_id": run.trace_id,
                            "conversation_id": conversation_id,
                            "from_cache": True,
                        }
                    )
                    await self._save_turn(ctx, conversation_id, question, resp)
                    self._audit_answer(ctx, resp)
                    return resp

            budget = self.gateway.new_budget()
            try:
                resp = await self._run(ctx, run, question, history_questions, conversation_id, mode, budget)
            except (GatewayExhaustedError, NoEligibleModelError, BudgetExceededError) as exc:
                await run.advance(Stage.FAILED, error=type(exc).__name__)
                span.fail(type(exc).__name__)
                resp = self._abstain(run, conversation_id, mode, f"model_unavailable:{type(exc).__name__}")
            span.set(
                status=str(resp.status),
                citations=len(resp.citations),
                cost_usd=round(budget.spent_cost_usd, 6),
            )
            await self._save_turn(ctx, conversation_id, question, resp)
            if cache_key and resp.status != AnswerStatus.ABSTAINED:
                await self._cache_put(ctx, cache_key, resp)
            self._audit_answer(ctx, resp)
            return resp

    async def _run(
        self,
        ctx: AuthzContext,
        run: WorkflowRun,
        question: str,
        history_questions: list[str],
        conversation_id: str,
        mode: ResponseMode,
        budget: RequestBudget,
    ) -> AnswerResponse:
        tracer = get_tracer()
        stats = RetrievalStats()
        with tracer.span("qa.plan"):
            plan, plan_route = await build_plan(
                self.gateway,
                question=question,
                history_questions=history_questions,
                cfg=self.cfg,
                acronyms=self.acronyms,
                budget=budget,
            )
        await run.advance(
            Stage.PLANNED, route=plan_route, subqueries=len(plan.subqueries), strategy=str(plan.strategy)
        )

        while True:
            with tracer.span("qa.retrieve", config_version=self.cfg.config_version) as sp:
                candidates = await self.retriever.search(ctx, plan, self.cfg, budget, stats)
                sp.set(bm25_hits=stats.bm25_hits, vector_hits=stats.vector_hits, fused=stats.fused)
            await run.advance(Stage.RETRIEVED, fused=stats.fused, deduped=stats.deduped)

            with tracer.span("qa.authorize") as sp:
                allowed = await self.retriever.recheck(ctx, candidates, stats)
                allowed = self.retriever.model_admissible(allowed, stats)
                sp.set(allowed=len(allowed), denied=dict(stats.denied_by_recheck))
            await run.advance(
                Stage.AUTHORIZED,
                allowed=len(allowed),
                denied=dict(stats.denied_by_recheck),
                excluded_by_model_policy=stats.excluded_by_model_policy,
            )

            passages: list[EvidencePassage] = []
            if allowed:
                with tracer.span("qa.rerank") as sp:
                    ranked = await self.retriever.rerank(
                        plan.standalone_question, allowed, self.cfg, budget, stats
                    )
                    sp.set(reranked=stats.reranked)
                await run.advance(Stage.RERANKED, reranked=stats.reranked)
                top = [c for c in ranked if (c.rerank_score or 0.0) >= self.cfg.min_rerank_score]
                expanded = await self.retriever.expand_context(
                    ctx, top[: self.cfg.final_passage_limit], self.cfg
                )
                passages = pack(top, self.cfg, expanded=expanded)
                stats.packed = len(passages)
                await run.advance(
                    Stage.PACKED,
                    passages=len(passages),
                    documents=sorted({(p.document_id, p.document_version) for p in passages}),
                )
            suff = assess_sufficiency(passages, plan, self.cfg)
            await run.advance(
                Stage.SUFFICIENCY_CHECKED, sufficient=suff.sufficient, reasons=list(suff.reasons)
            )
            if suff.sufficient:
                break
            if run.retrieval_retries >= self.cfg.max_retrieval_retries:
                await run.advance(Stage.ABSTAINED, reason="insufficient_evidence")
                return self._abstain(run, conversation_id, mode, "insufficient_evidence", stats=stats)
            plan = _broadened(plan)
            await run.advance(Stage.PLANNED, route="retry_broadened")

        classification = _classification(passages)
        validated, gen_route = await self._generate(question, plan, passages, classification, budget, None)
        await run.advance(Stage.GENERATED, model=gen_route)
        await run.advance(
            Stage.CITATIONS_VALIDATED,
            claims=len(validated.claims),
            citation_violations=len(validated.report.citation_violations),
            policy_violations=len(validated.report.policy_violations),
        )
        # Deterministic failure with nothing usable → one repair attempt, then abstain.
        if (
            not validated.claims
            and validated.status != "abstain"
            and run.repair_cycles < self.cfg.max_repair_cycles
        ):
            validated, gen_route = await self._generate(
                question,
                plan,
                passages,
                classification,
                budget,
                "Previous draft had invalid citations or policy violations.",
            )
            await run.advance(Stage.GENERATED, model=gen_route, repair=True)
            await run.advance(Stage.CITATIONS_VALIDATED, claims=len(validated.claims))
        if not validated.claims:
            await run.advance(Stage.ABSTAINED, reason=validated.abstain_reason or "no_valid_claims")
            return self._abstain(
                run, conversation_id, mode, validated.abstain_reason or "insufficient_evidence", stats=stats
            )

        judge_status = "not_run"
        verdict = None
        run_sync_judge = mode == ResponseMode.HIGH_ASSURANCE
        if run_sync_judge:
            outcome = await run_judge(
                self.gateway,
                self.rubric,
                question=question,
                claims=validated.claims,
                evidence=passages,
                classification=classification,
                budget=budget,
            )
            await run.advance(Stage.JUDGED, outcome=outcome.status, failure=outcome.failure)
            if (
                outcome.status == "revise"
                and run.repair_cycles < self.cfg.max_repair_cycles
                and outcome.verdict
            ):
                validated, gen_route = await self._generate(
                    question, plan, passages, classification, budget, repair_feedback(outcome.verdict)
                )
                await run.advance(Stage.GENERATED, model=gen_route, repair=True)
                await run.advance(Stage.CITATIONS_VALIDATED, claims=len(validated.claims))
                if not validated.claims:
                    await run.advance(Stage.ABSTAINED, reason="repair_produced_no_valid_claims")
                    return self._abstain(run, conversation_id, mode, "unsupported_after_repair", stats=stats)
                outcome = await run_judge(
                    self.gateway,
                    self.rubric,
                    question=question,
                    claims=validated.claims,
                    evidence=passages,
                    classification=classification,
                    budget=budget,
                )
                await run.advance(
                    Stage.JUDGED, outcome=outcome.status, failure=outcome.failure, after_repair=True
                )
                judge_status = "revised"
            validated, judge_status, abstain_reason = _apply_judge(validated, outcome, judge_status)
            verdict = outcome.verdict
            if abstain_reason:
                await run.advance(Stage.ABSTAINED, reason=abstain_reason)
                resp = self._abstain(run, conversation_id, mode, abstain_reason, stats=stats)
                return resp.model_copy(update={"judge": verdict, "judge_status": judge_status})
        elif random.random() < self.judge_sample_rate:  # noqa: S311 — sampling, not security
            judge_status = "sampled_async"
            self._spawn_async_judge(run.run_id, question, validated.claims, passages, classification)
        else:
            judge_status = "skipped"

        claims, citations = await resolve_citations(ctx.tenant_id, validated.claims, passages, self.store)
        status = AnswerStatus.ANSWERED if validated.status == "answered" else AnswerStatus.LIMITED
        if suff.limited:
            status = AnswerStatus.LIMITED
        outdated = sorted({c.title for c in citations if not c.is_latest_revision})
        await run.advance(Stage.RELEASED, status=str(status), citations=[c.citation_id for c in citations])
        return AnswerResponse(
            run_id=run.run_id,
            trace_id=run.trace_id,
            conversation_id=conversation_id,
            status=status,
            mode=mode,
            claims=claims,
            citations=citations,
            conflicts=validated.conflicts,
            outdated_sources=outdated,
            judge=verdict,
            judge_status=judge_status,
            config_versions=self._versions(),
            inference_mode=self.gateway.registry.inference_mode,
        )

    async def _generate(
        self,
        question: str,
        plan: RetrievalPlan,
        passages: list[EvidencePassage],
        classification: SensitivityLabel,
        budget: RequestBudget,
        feedback: str | None,
    ) -> tuple[ValidatedAnswer, str]:
        with get_tracer().span(
            "qa.generate", prompt_version=GENERATOR_PROMPT_VERSION, repair=feedback is not None
        ):
            result = await self.gateway.generate(
                GenerateRequest(
                    question=plan.standalone_question if plan.standalone_question else question,
                    evidence=passages,
                    prompt_version=GENERATOR_PROMPT_VERSION,
                    repair_feedback=feedback,
                ),
                classification=classification,
                budget=budget,
            )
        return validate_output(result.value, passages), result.record.model_key or "unknown"

    def _spawn_async_judge(
        self,
        run_id: str,
        question: str,
        claims: list[tuple[str, list[str]]],
        passages: list[EvidencePassage],
        classification: SensitivityLabel,
    ) -> None:
        async def job() -> None:
            async with self._bg_limit:
                outcome = await run_judge(
                    self.gateway,
                    self.rubric,
                    question=question,
                    claims=claims,
                    evidence=passages,
                    classification=classification,
                    budget=self.gateway.new_budget(),
                )
                if self.workflow_table is not None:
                    await self.workflow_table.put(
                        {"run_id": run_id, "step": "999:ASYNC_JUDGE"},
                        {
                            "outcome": outcome.status,
                            "failure": outcome.failure,
                            "verdict": outcome.verdict.model_dump() if outcome.verdict else None,
                        },
                        ttl=30 * 24 * 3600,
                    )

        task = asyncio.create_task(job())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def drain_background(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    def _versions(self) -> dict[str, str]:
        return {
            "retrieval": self.cfg.config_version,
            "models": self.gateway.registry.config_version,
            "rubric": self.rubric.rubric_version,
            "generator_prompt": GENERATOR_PROMPT_VERSION,
            "authz_policy": self.policy.policy_version,
        }

    def _abstain(
        self,
        run: WorkflowRun,
        conversation_id: str,
        mode: ResponseMode,
        reason: str,
        stats: RetrievalStats | None = None,
    ) -> AnswerResponse:
        return AnswerResponse(
            run_id=run.run_id,
            trace_id=run.trace_id,
            conversation_id=conversation_id,
            status=AnswerStatus.ABSTAINED,
            mode=mode,
            claims=[],
            citations=[],
            abstain_reason=reason,
            config_versions=self._versions(),
            inference_mode=self.gateway.registry.inference_mode,
        )

    def _audit_answer(self, ctx: AuthzContext, resp: AnswerResponse) -> None:
        self.audit.record(
            "qa.answer",
            actor=ctx.user_id,
            tenant_id=ctx.tenant_id,
            outcome=str(resp.status),
            resource=resp.run_id,
            documents=sorted({f"{c.document_id}@{c.document_version}" for c in resp.citations}),
            from_cache=resp.from_cache,
        )


def _apply_judge(
    validated: ValidatedAnswer, outcome: JudgeOutcome, judge_status: str
) -> tuple[ValidatedAnswer, str, str | None]:
    """High-assurance release rules. Returns (answer, judge_status, abstain_reason|None)."""
    if outcome.status == "failed" or outcome.verdict is None:
        return validated, "failed", "judge_unavailable"  # fail closed: nothing unjudged is released
    if outcome.status == "abstain":
        return validated, judge_status if judge_status == "revised" else "passed", "judge_abstained"
    if outcome.status == "pass":
        return validated, judge_status if judge_status == "revised" else "passed", None
    # Still "revise" after the single repair cycle: release only fully supported claims, clearly limited.
    keep = supported_subset(validated.claims, outcome.verdict)
    if not keep:
        return validated, "revised", "unsupported_after_repair"
    limited = ValidatedAnswer(
        status="limited",
        claims=keep,
        conflicts=validated.conflicts,
        abstain_reason=None,
        report=validated.report,
    )
    return limited, "revised", None


def _classification(passages: list[EvidencePassage]) -> SensitivityLabel:
    """Data classification of a model request = highest authoritative label among its evidence."""
    return max_of_labels([p.sensitivity_label for p in passages])


def _broadened(plan: RetrievalPlan) -> RetrievalPlan:
    """Retry plan: drop narrowing suggestions, query original + standalone, semantic emphasis."""
    subqueries = list(dict.fromkeys([plan.original_question, plan.standalone_question]))
    return plan.model_copy(
        update={
            "suggested_constraints": MetadataConstraints(),
            "subqueries": subqueries,
            "strategy": RetrievalStrategy.SEMANTIC_FIRST
            if not plan.exact_identifiers
            else RetrievalStrategy.HYBRID,
        }
    )


def _now_key() -> str:
    import time

    return f"{time.time_ns():020d}"
