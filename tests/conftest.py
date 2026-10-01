"""Shared fixtures.

Integration/e2e tests run against REAL local OpenSearch 3.1 and DynamoDB Local (``make up-deps``).
If those services are unreachable the tests ERROR (they are never silently skipped). Each session uses
an isolated table prefix and index namespace, removed afterwards.
"""

from __future__ import annotations

import secrets
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from erp_api.main import create_app
from erp_ingestion.pipeline import IngestionPipeline
from erp_ingestion.wiring import build_pipeline
from erp_observability.audit import AuditLog, MemorySink
from erp_rag.config import REPO_ROOT, Settings
from erp_rag.runtime import Core, build_core

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
ACME = "11111111-1111-4111-8111-111111111111"
GLOBEX = "22222222-2222-4222-8222-222222222222"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def require_services() -> None:
    missing = [
        n for n, p in (("OpenSearch", 9200), ("DynamoDB Local", 8001)) if not _reachable("127.0.0.1", p)
    ]
    if missing:
        pytest.fail(f"{', '.join(missing)} not reachable — run `make up-deps` first", pytrace=False)


def make_settings(tmp: Path, **overrides: Any) -> Settings:
    run = secrets.token_hex(4)
    base: dict[str, Any] = {
        "environment": "test",
        "table_prefix": f"erp-test-{run}-",
        "index_namespace": f"erptest-{run}",
        "artifact_root": tmp / "artifacts",
        "audit_dir": tmp / "audit",
        "dev_idp_key_path": tmp / "dev-idp.pem",
        "local_sources": {"localfs-synthetic": SYNTHETIC},
        "judge_sample_rate_standard": 0.0,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session")
def session_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("erp")


@pytest.fixture(scope="session")
def audit_sink() -> MemorySink:
    return MemorySink()


@pytest.fixture(scope="session")
async def core(session_tmp: Path, audit_sink: MemorySink) -> AsyncIterator[Core]:
    require_services()
    settings = make_settings(session_tmp)
    c = await build_core(settings, audit=AuditLog([audit_sink]))
    yield c
    await c.index.drop()
    c.db.drop_tables()
    await c.close()


@pytest.fixture(scope="session")
async def pipeline(core: Core) -> IngestionPipeline:
    return build_pipeline(core, local_roots={"localfs-synthetic": SYNTHETIC})


@pytest.fixture(scope="session")
async def ingested(pipeline: IngestionPipeline) -> list[Any]:
    results = await pipeline.sync("localfs-synthetic")
    assert results, "synthetic corpus produced no events"
    assert not [r for r in results if r.outcome == "failed"], results
    return results


@pytest.fixture(scope="session")
async def client(core: Core, ingested: list[Any]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(core.settings, core=core)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


class Users:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._tokens: dict[str, str] = {}

    async def token(self, username: str) -> str:
        if username not in self._tokens:
            resp = await self._client.post("/dev-idp/token", json={"username": username})
            resp.raise_for_status()
            self._tokens[username] = resp.json()["access_token"]
        return self._tokens[username]

    async def headers(self, username: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token(username)}"}


@pytest.fixture(scope="session")
async def users(client: httpx.AsyncClient) -> Users:
    return Users(client)
