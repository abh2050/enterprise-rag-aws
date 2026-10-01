"""FastAPI application.

Identity verification (token → ``VerifiedIdentity``) and authorization (``AuthzContext`` + policy) are
separate dependencies. Request models forbid unknown fields, so clients cannot smuggle tenant, group,
clearance or filter values into a request.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from erp_auth.context import ContextBuilder, EntitlementMap
from erp_auth.devidp import DevIdentityProvider, load_dev_directory
from erp_auth.directory import DirectoryAdapter, DirectoryUnavailable, SyntheticDirectory
from erp_auth.entra import build_entra_verifier
from erp_auth.models import AuthzContext, VerifiedIdentity
from erp_auth.policy import decide
from erp_auth.tokens import TokenValidationError, TokenVerifier
from erp_ingestion.pipeline import IngestionPipeline
from erp_ingestion.wiring import build_artifacts, build_pipeline
from erp_observability.tracing import trace_context
from erp_rag.config import Settings
from erp_rag.runtime import Core, build_core
from erp_rag.schemas import AnswerResponse, ResponseMode
from erp_rag.service import QuestionRejectedError

log = logging.getLogger("erp.api")
NOT_FOUND = {"detail": "not found"}  # identical for missing and unauthorized (no existence oracle)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    mode: ResponseMode = ResponseMode.STANDARD


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^run_[0-9a-f]{24}$")
    rating: Literal["up", "down"]
    reason: Literal["incorrect", "unsupported", "incomplete", "outdated", "irrelevant", "other"] | None = None
    comment: str | None = Field(default=None, max_length=1000)


class DevTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str


class AdminActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class AppState:
    core: Core
    verifier: TokenVerifier
    contexts: ContextBuilder
    pipeline: IngestionPipeline
    dev_idp: DevIdentityProvider | None = None
    http: httpx.AsyncClient


def create_app(settings: Settings | None = None, *, core: Core | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.assert_safe_for_environment()  # fail startup before serving anything
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state.http = httpx.AsyncClient()
        state.core = core or await build_core(settings)
        entitlements = EntitlementMap.load(settings.entitlements_path)
        directory: DirectoryAdapter
        if settings.auth_provider == "dev":
            dev_dir = load_dev_directory(settings.dev_directory_path)
            state.dev_idp = DevIdentityProvider(
                issuer=settings.dev_idp_issuer,
                audience=settings.api_audience,
                directory=dev_dir,
                key_path=settings.dev_idp_key_path,
            )
            state.verifier = state.dev_idp.verifier()
            directory = SyntheticDirectory(dev_dir)
        else:
            from erp_auth.graph import graph_directory_from_settings

            state.verifier = build_entra_verifier(
                api_client_id=settings.entra_api_client_id or "",
                allowed_tenants=frozenset(settings.entra_allowed_tenants),
                required_scopes=frozenset(settings.entra_required_scopes),
                accepted_app_roles=frozenset(settings.entra_accepted_app_roles),
                http=state.http,
            )
            directory = graph_directory_from_settings(settings, state.http)
        state.contexts = ContextBuilder(directory, entitlements)
        roots = {k: v for k, v in settings.local_sources.items() if v.exists()} if settings.is_local else {}
        state.pipeline = build_pipeline(state.core, local_roots=roots, http=state.http)
        yield
        await state.core.close()
        await state.http.aclose()

    app = FastAPI(
        title="enterprise-rag-platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.is_local else None,
        redoc_url=None,
    )
    app.state.erp = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def trace_and_headers(request: Request, call_next: Any) -> Response:
        incoming = request.headers.get("x-trace-id")
        with trace_context(incoming) as trace_id:
            response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    # ------------------------------------------------------------------ dependencies

    async def identity(request: Request) -> VerifiedIdentity:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return await state.verifier.verify(token.strip())
        except TokenValidationError as exc:
            state.core.audit.record(
                "auth.reject", actor=None, tenant_id=None, outcome="denied", reason=exc.code
            )
            raise HTTPException(
                401, "invalid token", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            ) from exc

    async def authz(ident: Annotated[VerifiedIdentity, Depends(identity)]) -> AuthzContext:
        try:
            return await state.contexts.build(ident)
        except DirectoryUnavailable as exc:
            raise HTTPException(503, "group membership could not be resolved") from exc

    Ctx = Annotated[AuthzContext, Depends(authz)]

    def require_admin(ctx: AuthzContext) -> None:
        if not ctx.is_admin:
            state.core.audit.record(
                "admin.denied", actor=ctx.user_id, tenant_id=ctx.tenant_id, outcome="denied"
            )
            raise HTTPException(403, "admin role required")

    # ------------------------------------------------------------------ routes

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            await state.core.os_client.cluster.health()
            checks["opensearch"] = "ok"
        except Exception:
            checks["opensearch"] = "unavailable"
        try:
            await state.core.store.get("__readiness__", "__probe__")
            checks["dynamodb"] = "ok"
        except Exception:
            checks["dynamodb"] = "unavailable"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
        )

    @app.get("/api/me")
    async def me(ctx: Ctx, ident: Annotated[VerifiedIdentity, Depends(identity)]) -> dict[str, Any]:
        return {
            "display_name": ident.display_name,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "group_count": sum(1 for p in ctx.principals if p.startswith("group:")),
            "groups_from_directory": ident.groups_overage,
            "clearance": ctx.clearance,
            "projects": sorted(ctx.projects),
            "is_admin": ctx.is_admin,
            "auth_provider": ident.provider,
            "inference_mode": state.core.gateway.registry.inference_mode,
        }

    @app.post("/api/ask", response_model=AnswerResponse)
    async def ask(body: AskRequest, ctx: Ctx) -> AnswerResponse:
        try:
            return await state.core.qa.ask(
                ctx, body.question, conversation_id=body.conversation_id, mode=body.mode
            )
        except QuestionRejectedError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str, ctx: Ctx) -> dict[str, Any]:
        if not conversation_id.replace("-", "").replace("_", "").isalnum() or len(conversation_id) > 64:
            raise HTTPException(404, "not found")
        turns = await state.core.qa.load_history(ctx, conversation_id)
        return {
            "conversation_id": conversation_id,
            "turns": [
                {
                    "question": t.question,
                    "status": t.status,
                    "answer_text": t.answer_text,
                    "withheld": t.withheld,
                }
                for t in turns
            ],
        }

    @app.get("/api/citations/{chunk_id}")
    async def citation(chunk_id: str, ctx: Ctx) -> Any:
        chunk = await state.core.index.get_chunk(chunk_id) if chunk_id.startswith("chk_") else None
        record = await state.core.store.get(ctx.tenant_id, chunk.document_id) if chunk else None
        allowed = (
            chunk is not None
            and chunk.tenant_id == ctx.tenant_id
            and decide(ctx, record, settings=state.core.policy, version=chunk.document_version).allowed
        )
        state.core.audit.record(
            "citation.open",
            actor=ctx.user_id,
            tenant_id=ctx.tenant_id,
            outcome="allowed" if allowed else "denied",
            resource=chunk_id,
        )
        if not allowed or chunk is None or record is None:
            return JSONResponse(NOT_FOUND, status_code=404)
        return {
            "citation_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "document_version": chunk.document_version,
            "title": record.title,
            "section": chunk.section,
            "location": chunk.location,
            "text": chunk.content,
            "source_uri": record.source_uri,
            "source_modified_at": record.source_modified_at,
            "acl_synced_at": record.acl_synced_at,
            "sensitivity_label": record.sensitivity_label,
            "download_url": f"/api/documents/{chunk.document_id}/download",
        }

    @app.get("/api/documents/{document_id}/download")
    async def download(document_id: str, ctx: Ctx) -> Response:
        record = (
            await state.core.store.get(ctx.tenant_id, document_id) if document_id.startswith("doc_") else None
        )
        allowed = decide(ctx, record, settings=state.core.policy).allowed
        state.core.audit.record(
            "document.download",
            actor=ctx.user_id,
            tenant_id=ctx.tenant_id,
            outcome="allowed" if allowed else "denied",
            resource=document_id,
        )
        if not allowed or record is None or record.current_version is None:
            return JSONResponse(NOT_FOUND, status_code=404)
        manifest = await state.core.tables["chunk_manifest"].get(
            {"document_id": document_id, "document_version": record.current_version}
        )
        data = await build_artifacts(state.core).get(manifest["landing_key"]) if manifest else None
        if data is None:
            return JSONResponse(NOT_FOUND, status_code=404)
        filename = "".join(
            c for c in (manifest or {}).get("filename", "document") if c.isalnum() or c in "-_."
        )
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/feedback", status_code=201)
    async def feedback(body: FeedbackRequest, ctx: Ctx) -> dict[str, str]:
        fid = f"{datetime.now(UTC).isoformat()}#{body.run_id}"
        await state.core.tables["feedback"].put(
            {"tenant_user": f"{ctx.tenant_id}#{ctx.user_id}", "feedback_id": fid},
            {"run_id": body.run_id, "rating": body.rating, "reason": body.reason, "comment": body.comment},
        )
        state.core.audit.record(
            "feedback.submit",
            actor=ctx.user_id,
            tenant_id=ctx.tenant_id,
            outcome="stored",
            resource=body.run_id,
            rating=body.rating,
        )
        return {"status": "stored"}

    # ------------------------------------------------------------------ admin (same-tenant only)

    @app.get("/api/admin/documents")
    async def admin_list(ctx: Ctx) -> dict[str, Any]:
        require_admin(ctx)
        records = await state.core.store.list_tenant(ctx.tenant_id)
        return {
            "documents": [
                {
                    "document_id": r.document_id,
                    "title": r.title,
                    "status": r.status,
                    "current_version": r.current_version,
                    "revoked": r.revoked,
                    "acl_version": r.acl_version,
                    "sensitivity_label": r.sensitivity_label,
                    "acl_synced_at": r.acl_synced_at,
                    "governance_source": r.governance_source,
                    "legal_hold": r.legal_hold,
                }
                for r in records
            ]
        }

    @app.post("/api/admin/documents/{document_id}/revoke")
    async def admin_revoke(document_id: str, body: AdminActionRequest, ctx: Ctx) -> dict[str, Any]:
        require_admin(ctx)
        try:
            rec = await state.pipeline.revoke(
                ctx.tenant_id, document_id, actor=ctx.user_id, reason=body.reason
            )
        except KeyError:
            return JSONResponse(NOT_FOUND, status_code=404)  # type: ignore[return-value]
        return {"document_id": document_id, "revoked": rec.revoked, "acl_version": rec.acl_version}

    @app.delete("/api/admin/documents/{document_id}")
    async def admin_delete(document_id: str, body: AdminActionRequest, ctx: Ctx) -> dict[str, Any]:
        require_admin(ctx)
        try:
            rec = await state.pipeline.tombstone(
                ctx.tenant_id, document_id, actor=ctx.user_id, reason=body.reason
            )
        except KeyError:
            return JSONResponse(NOT_FOUND, status_code=404)  # type: ignore[return-value]
        return {"document_id": document_id, "status": rec.status, "legal_hold": rec.legal_hold}

    @app.post("/api/admin/documents/{document_id}/replay")
    async def admin_replay(document_id: str, ctx: Ctx) -> dict[str, Any]:
        require_admin(ctx)
        if settings.ingestion_state_machine_arn:
            # AWS: the worker (with the malware scanner sidecar) performs the replay.
            record = await state.core.store.get(ctx.tenant_id, document_id)
            if record is None:
                return JSONResponse(NOT_FOUND, status_code=404)  # type: ignore[return-value]
            execution = await start_replay_execution(
                settings.ingestion_state_machine_arn,
                settings.aws_region,
                ctx.tenant_id,
                document_id,
                ctx.user_id,
            )
            state.core.audit.record(
                "ingest.replay",
                actor=ctx.user_id,
                tenant_id=ctx.tenant_id,
                outcome="queued",
                resource=document_id,
            )
            return {"document_id": document_id, "outcome": "queued", "detail": execution}
        try:
            result = await state.pipeline.replay_document(ctx.tenant_id, document_id, actor=ctx.user_id)
        except KeyError:
            return JSONResponse(NOT_FOUND, status_code=404)  # type: ignore[return-value]
        return {"document_id": document_id, "outcome": result.outcome, "detail": result.detail}

    # ------------------------------------------------------------------ development identity provider

    if settings.auth_provider == "dev":
        if not settings.is_local:  # defence in depth; assert_safe_for_environment already refuses this
            raise RuntimeError("dev identity provider cannot be mounted outside local/test")

        @app.get("/dev-idp/users")
        async def dev_users() -> dict[str, Any]:
            assert state.dev_idp is not None
            return {
                "synthetic": True,
                "users": [
                    {"username": u.username, "display_name": u.display_name, "description": u.description}
                    for u in state.dev_idp.users
                ],
            }

        @app.post("/dev-idp/token")
        async def dev_token(body: DevTokenRequest) -> dict[str, Any]:
            assert state.dev_idp is not None
            if state.dev_idp.user(body.username) is None:
                raise HTTPException(404, "unknown synthetic user")
            return {
                "access_token": state.dev_idp.issue_token(body.username),
                "token_type": "Bearer",
                "expires_in": 3600,
            }

        @app.get("/dev-idp/jwks")
        async def dev_jwks() -> dict[str, Any]:
            assert state.dev_idp is not None
            return state.dev_idp.jwks()

    return app


async def start_replay_execution(
    state_machine_arn: str, region: str, tenant_id: str, document_id: str, actor: str
) -> str:
    import asyncio
    import json

    import boto3

    client = boto3.client("stepfunctions", region_name=region)
    body = json.dumps({"kind": "replay", "tenant_id": tenant_id, "document_id": document_id, "actor": actor})
    resp = await asyncio.to_thread(
        client.start_execution, stateMachineArn=state_machine_arn, input=json.dumps([{"body": body}])
    )
    return str(resp["executionArn"]).rsplit(":", 1)[-1]


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()
