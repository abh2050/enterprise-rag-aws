"""Amazon Bedrock provider.

* plan / generate / judge: ``bedrock-runtime`` Converse API (JSON-only output, temperature 0). The
  ``thinking`` request field is sent only to Anthropic models (Nova rejects unknown fields).
* embed: ``InvokeModel`` with Titan Text Embeddings V2 body ``{inputText, dimensions, normalize}``.
* rerank: ``bedrock-agent-runtime`` Rerank API for dedicated rerank models; otherwise an LLM scores the
  passages via Converse (``rerank-prompt-v1``) — used where Bedrock Rerank is unavailable (us-east-2) or
  denied by organization policy.

Clients are created per region, and only for regions on the allowlist (the registry's approved regions).
API shapes verified 2026-09-30 against AWS docs and the installed botocore service model.
Live status: Titan V2 embedding invoked successfully in us-east-2 (2026-10-01); other paths contract-tested.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from erp_rag.gateway.types import (
    GenerateRequest,
    JudgeRequest,
    ModelSpec,
    PlanRequest,
    ProviderError,
    RetryableProviderError,
    ThrottledError,
    Usage,
)
from erp_rag.prompts import generator_messages, judge_messages, planner_messages, rerank_messages

_RETRYABLE = {
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
    "ModelNotReadyException",
}
_THROTTLE = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE:
            return ThrottledError(code)
        if code in _RETRYABLE:
            return RetryableProviderError(code)
        err = ProviderError(code or "client_error")
        return err
    if isinstance(exc, BotoCoreError):
        return RetryableProviderError(type(exc).__name__)
    return ProviderError(type(exc).__name__)


RERANK_MODEL_PREFIXES = ("amazon.rerank", "cohere.rerank")


class BedrockProvider:
    def __init__(
        self,
        *,
        region: str,
        allowed_regions: frozenset[str] | None = None,
        runtime: Any = None,
        agent_runtime: Any = None,
    ) -> None:
        # Client-side retries disabled: the gateway owns retry/backoff/deadline policy.
        self._cfg = Config(
            retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=5, read_timeout=60
        )
        self.region = region
        self.allowed_regions = allowed_regions or frozenset({region})
        self._runtimes: dict[str, Any] = {region: runtime} if runtime is not None else {}
        self._agents: dict[str, Any] = {region: agent_runtime} if agent_runtime is not None else {}

    def _check_region(self, spec: ModelSpec) -> None:
        # Never call a region the deployment has not approved (data residency + organization policy).
        if spec.region not in self.allowed_regions:
            raise ProviderError("region_not_allowed")

    def _runtime(self, region: str) -> Any:
        if region not in self._runtimes:
            self._runtimes[region] = boto3.client("bedrock-runtime", region_name=region, config=self._cfg)
        return self._runtimes[region]

    def _agent(self, region: str) -> Any:
        if region not in self._agents:
            self._agents[region] = boto3.client("bedrock-agent-runtime", region_name=region, config=self._cfg)
        return self._agents[region]

    async def _converse(self, spec: ModelSpec, system: str, user: str) -> tuple[str, Usage]:
        self._check_region(spec)

        kwargs: dict[str, Any] = {
            "modelId": spec.model_id,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": spec.max_output_tokens, "temperature": 0.0},
        }
        if "anthropic." in spec.model_id:
            kwargs["additionalModelRequestFields"] = {"thinking": {"type": "disabled"}}
        client = self._runtime(spec.region)

        def call() -> dict[str, Any]:
            result: dict[str, Any] = client.converse(**kwargs)
            return result

        try:
            resp = await asyncio.to_thread(call)
        except Exception as exc:
            raise _map_error(exc) from exc
        parts = resp.get("output", {}).get("message", {}).get("content", [])
        text = "".join(p.get("text", "") for p in parts)
        usage = resp.get("usage", {})
        return text, Usage(
            input_tokens=int(usage.get("inputTokens", 0)), output_tokens=int(usage.get("outputTokens", 0))
        )

    async def plan(self, spec: ModelSpec, req: PlanRequest) -> tuple[str, Usage]:
        system, user = planner_messages(req)
        return await self._converse(spec, system, user)

    async def generate(self, spec: ModelSpec, req: GenerateRequest) -> tuple[str, Usage]:
        system, user = generator_messages(req)
        return await self._converse(spec, system, user)

    async def judge(self, spec: ModelSpec, req: JudgeRequest) -> tuple[str, Usage]:
        system, user = judge_messages(req)
        return await self._converse(spec, system, user)

    async def embed(
        self, spec: ModelSpec, texts: list[str], purpose: Literal["query", "document"]
    ) -> tuple[list[list[float]], Usage]:
        self._check_region(spec)
        dim = spec.embedding_dimension or 1024
        client = self._runtime(spec.region)

        def one(text: str) -> tuple[list[float], int]:
            resp = client.invoke_model(
                modelId=spec.model_id,
                body=json.dumps({"inputText": text[:30000], "dimensions": dim, "normalize": True}),
                contentType="application/json",
                accept="application/json",
            )
            body = json.loads(resp["body"].read())
            return [float(x) for x in body["embedding"]], int(body.get("inputTextTokenCount", 0))

        try:
            results = await asyncio.gather(*(asyncio.to_thread(one, t) for t in texts))
        except Exception as exc:
            raise _map_error(exc) from exc
        return [r[0] for r in results], Usage(input_tokens=sum(r[1] for r in results))

    async def rerank(self, spec: ModelSpec, query: str, documents: list[str]) -> tuple[list[float], Usage]:
        self._check_region(spec)
        if not spec.model_id.startswith(RERANK_MODEL_PREFIXES):
            return await self._llm_rerank(spec, query, documents)
        arn = f"arn:aws:bedrock:{spec.region}::foundation-model/{spec.model_id}"
        agent = self._agent(spec.region)

        def call() -> dict[str, Any]:
            result: dict[str, Any] = agent.rerank(
                queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                sources=[
                    {
                        "type": "INLINE",
                        "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d[:8000]}},
                    }
                    for d in documents
                ],
                rerankingConfiguration={
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "numberOfResults": len(documents),
                        "modelConfiguration": {"modelArn": arn},
                    },
                },
            )
            return result

        try:
            resp = await asyncio.to_thread(call)
        except Exception as exc:
            raise _map_error(exc) from exc
        scores = [0.0] * len(documents)
        for item in resp.get("results", []):
            idx = int(item["index"])
            if 0 <= idx < len(scores):
                scores[idx] = float(item["relevanceScore"])
        return scores, Usage(input_tokens=(len(query) + sum(len(d) for d in documents)) // 4)

    async def _llm_rerank(
        self, spec: ModelSpec, query: str, documents: list[str]
    ) -> tuple[list[float], Usage]:
        """Score passages with a chat model. Passages are untrusted data; output is schema-checked."""
        system, user = rerank_messages(query, documents)
        text, usage = await self._converse(spec, system, user)
        start, end = text.find("{"), text.rfind("}")
        try:
            raw = json.loads(text[start : end + 1])["scores"]
            scores = [float(x) for x in raw]
        except (ValueError, KeyError, TypeError) as exc:
            raise RetryableProviderError("invalid_rerank_output") from exc
        if len(scores) != len(documents):
            raise RetryableProviderError("invalid_rerank_output")
        return scores, usage
