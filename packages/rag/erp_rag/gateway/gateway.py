"""The single model gateway. Every plan/embed/rerank/generate/judge call goes through here.

Enforced per call: approved model list, data-classification gate, context capacity, modality, deadline,
token + cost budget, per-model concurrency, bounded retries with jittered backoff, circuit breaker,
fallback strictly within the approved chain. Every call yields a persisted ``RouteRecord``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from erp_auth.models import LABEL_RANK, SensitivityLabel
from erp_observability.tracing import get_tracer
from erp_rag.gateway.types import (
    Budgets,
    GenerateRequest,
    JudgeRequest,
    ModelProvider,
    ModelSpec,
    PlanDraft,
    PlanRequest,
    ProviderError,
    RetryableProviderError,
    RouteRecord,
    Task,
    TaskRoute,
    Usage,
)
from erp_rag.schemas import GeneratorOutput, JudgeVerdict

T = TypeVar("T")
R = TypeVar("R")


class ModelRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: str
    inference_mode: Literal["fixture", "live"]
    models: dict[str, ModelSpec]
    routes: dict[Task, TaskRoute]
    budgets: Budgets = Budgets()

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        raw = yaml.safe_load(path.read_text())
        for key, spec in raw.get("models", {}).items():
            spec.setdefault("key", key)
        registry = cls.model_validate(raw)
        registry.validate_chains()
        return registry

    def validate_chains(self) -> None:
        for task, route in self.routes.items():
            for key in route.chain:
                spec = self.models.get(key)
                if spec is None:
                    raise ValueError(f"route {task} references unknown model {key}")
                if task not in spec.tasks:
                    raise ValueError(f"model {key} is not approved for task {task}")
        embed = self.routes.get(Task.EMBED)
        if embed:
            versions = {self.models[k].embedding_version for k in embed.chain}
            if len(versions) != 1 or None in versions:
                # Falling back to a different embedding model would query an incompatible index.
                raise ValueError("embedding chain must share exactly one embedding_version")

    @property
    def embedding_spec(self) -> ModelSpec:
        return self.models[self.routes[Task.EMBED].chain[0]]


class NoEligibleModelError(Exception):
    def __init__(self, task: Task, reasons: list[str]) -> None:
        super().__init__(f"no eligible model for {task}: {reasons}")
        self.task = task
        self.reasons = reasons


class BudgetExceededError(Exception):
    pass


class GatewayExhaustedError(Exception):
    """All approved models failed (retries exhausted)."""

    def __init__(self, task: Task, record: RouteRecord) -> None:
        super().__init__(f"{task}: all approved models failed ({record.error_category})")
        self.task = task
        self.record = record


class OutputValidationError(RetryableProviderError):
    category = "invalid_output"


@dataclass
class RequestBudget:
    max_cost_usd: float
    max_tokens: int
    spent_cost_usd: float = 0.0
    spent_tokens: int = 0

    def check(self, est_cost: float, est_tokens: int) -> None:
        if self.spent_cost_usd + est_cost > self.max_cost_usd:
            raise BudgetExceededError("cost budget exceeded")
        if self.spent_tokens + est_tokens > self.max_tokens:
            raise BudgetExceededError("token budget exceeded")

    def add(self, cost: float, tokens: int) -> None:
        self.spent_cost_usd += cost
        self.spent_tokens += tokens


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    failures: int = 0
    opened_at: float | None = None
    half_open_inflight: bool = False

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_seconds and not self.half_open_inflight:
            self.half_open_inflight = True
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.half_open_inflight = False

    def failure(self) -> None:
        self.failures += 1
        self.half_open_inflight = False
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


RouteSink = Callable[[RouteRecord], Awaitable[None]]


@dataclass
class GatewayResult[R]:
    value: R
    record: RouteRecord
    spec: ModelSpec


@dataclass
class ModelGateway:
    registry: ModelRegistry
    providers: dict[str, ModelProvider]
    route_sink: RouteSink | None = None
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def new_budget(self) -> RequestBudget:
        b = self.registry.budgets
        return RequestBudget(max_cost_usd=b.max_cost_usd_per_request, max_tokens=b.max_tokens_per_request)

    # ---------------------------------------------------------------- typed task methods

    async def plan(self, req: PlanRequest, *, budget: RequestBudget) -> GatewayResult[PlanDraft]:
        est = sum(len(q) for q in [req.question, *req.history_questions]) // 4 + 400
        return await self._execute(
            Task.PLAN,
            classification=SensitivityLabel.INTERNAL,  # questions are treated as internal data
            est_input_tokens=est,
            budget=budget,
            call=lambda spec, p: p.plan(spec, req),
            parse=lambda raw: _parse_json_model(raw, PlanDraft),
        )

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: Literal["query", "document"],
        classification: SensitivityLabel,
        budget: RequestBudget | None = None,
    ) -> GatewayResult[list[list[float]]]:
        est = sum(len(t) for t in texts) // 4 + 1
        spec_dim = self.registry.embedding_spec.embedding_dimension

        def check(vectors: list[list[float]]) -> list[list[float]]:
            if len(vectors) != len(texts) or any(len(v) != spec_dim for v in vectors):
                raise OutputValidationError("embedding shape mismatch")
            return vectors

        return await self._execute(
            Task.EMBED,
            classification=classification,
            est_input_tokens=est,
            budget=budget or self.new_budget(),
            call=lambda spec, p: p.embed(spec, texts, purpose),
            parse=check,
        )

    async def rerank(
        self, query: str, documents: list[str], *, classification: SensitivityLabel, budget: RequestBudget
    ) -> GatewayResult[list[float]]:
        est = (len(query) + sum(len(d) for d in documents)) // 4 + 1

        def check(scores: list[float]) -> list[float]:
            if len(scores) != len(documents):
                raise OutputValidationError("rerank length mismatch")
            return [min(1.0, max(0.0, float(s))) for s in scores]

        return await self._execute(
            Task.RERANK,
            classification=classification,
            est_input_tokens=est,
            budget=budget,
            call=lambda spec, p: p.rerank(spec, query, documents),
            parse=check,
        )

    async def generate(
        self, req: GenerateRequest, *, classification: SensitivityLabel, budget: RequestBudget
    ) -> GatewayResult[GeneratorOutput]:
        est = (len(req.question) + sum(len(e.text) for e in req.evidence)) // 4 + 600
        return await self._execute(
            Task.GENERATE,
            classification=classification,
            est_input_tokens=est,
            budget=budget,
            call=lambda spec, p: p.generate(spec, req),
            parse=lambda raw: _parse_json_model(raw, GeneratorOutput),
        )

    async def judge(
        self, req: JudgeRequest, *, classification: SensitivityLabel, budget: RequestBudget
    ) -> GatewayResult[JudgeVerdict]:
        est = (len(req.question) + sum(len(e.text) for e in req.evidence) + len(req.rubric_text)) // 4 + 800
        return await self._execute(
            Task.JUDGE,
            classification=classification,
            est_input_tokens=est,
            budget=budget,
            call=lambda spec, p: p.judge(spec, req),
            parse=lambda raw: _parse_json_model(raw, JudgeVerdict),
        )

    # ---------------------------------------------------------------- core execution

    def eligible(
        self, task: Task, classification: SensitivityLabel, est_input_tokens: int
    ) -> tuple[list[ModelSpec], list[str]]:
        route = self.registry.routes.get(task)
        if route is None:
            return [], [f"no route for {task}"]
        eligible: list[ModelSpec] = []
        skipped: list[str] = []
        for key in route.chain:
            spec = self.registry.models[key]
            if not spec.approved:
                skipped.append(f"{key}:not_approved")
            elif task not in spec.tasks:
                skipped.append(f"{key}:task_not_approved")
            elif LABEL_RANK[classification] > LABEL_RANK[spec.max_classification]:
                skipped.append(f"{key}:classification_{classification}_exceeds_{spec.max_classification}")
            elif est_input_tokens + spec.max_output_tokens > spec.context_tokens:
                skipped.append(f"{key}:context_capacity")
            elif "text" not in spec.modalities:
                skipped.append(f"{key}:modality")
            elif spec.provider not in self.providers:
                skipped.append(f"{key}:provider_unavailable")
            else:
                eligible.append(spec)
        return eligible, skipped

    async def _execute(
        self,
        task: Task,
        *,
        classification: SensitivityLabel,
        est_input_tokens: int,
        budget: RequestBudget,
        call: Callable[[ModelSpec, ModelProvider], Awaitable[tuple[T, Usage]]],
        parse: Callable[[T], R],
    ) -> GatewayResult[R]:
        route = self.registry.routes[task]
        eligible, skipped = self.eligible(task, classification, est_input_tokens)
        started = time.monotonic()
        deadline = started + route.deadline_ms / 1000
        tracer = get_tracer()
        if not eligible:
            await self._record(
                RouteRecord(
                    task=task,
                    model_key=None,
                    model_id=None,
                    provider=None,
                    route_reason="no_eligible_model",
                    config_version=self.registry.config_version,
                    attempts=0,
                    latency_ms=0.0,
                    usage=Usage(),
                    est_cost_usd=0.0,
                    outcome="failed",
                    fallback_used=False,
                    error_category="no_eligible_model",
                    skipped=skipped,
                )
            )
            raise NoEligibleModelError(task, skipped)

        attempts = 0
        last_error = "unknown"
        with tracer.span(f"gateway.{task}", task=str(task), classification=str(classification)) as span:
            for position, spec in enumerate(eligible):
                breaker = self.breakers.setdefault(spec.key, CircuitBreaker())
                if not breaker.allow():
                    skipped.append(f"{spec.key}:circuit_open")
                    last_error = "circuit_open"
                    continue
                est_cost = _cost(spec, est_input_tokens, spec.max_output_tokens)
                budget.check(est_cost, est_input_tokens + spec.max_output_tokens)
                sem = self._semaphores.setdefault(spec.key, asyncio.Semaphore(route.max_concurrency))
                for attempt in range(route.max_retries + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        last_error = "deadline_exceeded"
                        break
                    attempts += 1
                    try:
                        async with asyncio.timeout(min(remaining, route.attempt_timeout_ms / 1000)), sem:
                            raw, usage = await call(spec, self.providers[spec.provider])
                        value = parse(raw)
                    except TimeoutError:
                        last_error = "timeout"
                        breaker.failure()
                    except (OutputValidationError, ValidationError, ValueError):
                        last_error = "invalid_output"
                        breaker.failure()
                    except RetryableProviderError as exc:
                        last_error = exc.category
                        breaker.failure()
                    except ProviderError as exc:
                        last_error = exc.category
                        breaker.failure()
                        break  # non-retryable on this model → next approved fallback
                    else:
                        breaker.success()
                        cost = _cost(spec, usage.input_tokens, usage.output_tokens)
                        budget.add(cost, usage.input_tokens + usage.output_tokens)
                        record = RouteRecord(
                            task=task,
                            model_key=spec.key,
                            model_id=spec.model_id,
                            provider=spec.provider,
                            route_reason="primary" if position == 0 else f"fallback_after:{last_error}",
                            config_version=self.registry.config_version,
                            attempts=attempts,
                            latency_ms=(time.monotonic() - started) * 1000,
                            usage=usage,
                            est_cost_usd=cost,
                            outcome="ok",
                            fallback_used=position > 0,
                            skipped=skipped,
                        )
                        span.set(
                            model=spec.key,
                            attempts=attempts,
                            fallback=position > 0,
                            cost_usd=round(cost, 6),
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                        )
                        await self._record(record)
                        return GatewayResult(value=value, record=record, spec=spec)
                    if attempt < route.max_retries:
                        backoff = route.backoff_base_ms / 1000 * (2**attempt) * (0.5 + random.random())  # noqa: S311
                        await asyncio.sleep(max(0.0, min(backoff, deadline - time.monotonic())))
            record = RouteRecord(
                task=task,
                model_key=None,
                model_id=None,
                provider=None,
                route_reason="exhausted",
                config_version=self.registry.config_version,
                attempts=attempts,
                latency_ms=(time.monotonic() - started) * 1000,
                usage=Usage(),
                est_cost_usd=0.0,
                outcome="failed",
                fallback_used=len(eligible) > 1,
                error_category=last_error,
                skipped=skipped,
            )
            span.fail(last_error)
            await self._record(record)
            raise GatewayExhaustedError(task, record)

    async def _record(self, record: RouteRecord) -> None:
        if self.route_sink is None:
            return
        try:
            await self.route_sink(record)
        except Exception:  # ledger failures must not break inference
            logging.getLogger("erp.gateway").warning("route ledger write failed", exc_info=False)


def _cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    return input_tokens / 1000 * spec.price_input_per_1k + output_tokens / 1000 * spec.price_output_per_1k


def _parse_json_model[M: BaseModel](raw: str, model: type[M]) -> M:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise OutputValidationError("no JSON object in model output")
    try:
        return model.model_validate_json(text[start : end + 1])
    except ValidationError as exc:
        raise OutputValidationError(f"schema validation failed: {exc.error_count()} errors") from exc
