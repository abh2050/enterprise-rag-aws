"""Microsoft Graph app-only token acquisition (client credentials) for the directory adapter.

Client secret is read from AWS Secrets Manager (never from source or plain env in non-local
environments). Live status: implemented-not-live-tested.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from erp_auth.directory import DirectoryAdapter, DirectoryUnavailable, GraphDirectory

if TYPE_CHECKING:
    from erp_rag.config import Settings

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105 — URL, not a secret
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class ClientCredentialTokens:
    def __init__(
        self, http: httpx.AsyncClient, client_id: str, secret_loader: Any, *, scope: str = GRAPH_SCOPE
    ) -> None:
        self._scope = scope
        self._http = http
        self._client_id = client_id
        self._secret_loader = secret_loader
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, tenant_id: str) -> str:
        cached = self._cache.get(tenant_id)
        if cached and cached[0] - 60 > time.time():
            return cached[1]
        async with self._lock:
            secret = await self._secret_loader()
            resp = await self._http.post(
                TOKEN_URL.format(tenant=tenant_id),
                data={
                    "client_id": self._client_id,
                    "client_secret": secret,
                    "scope": self._scope,
                    "grant_type": "client_credentials",
                },
                timeout=10.0,
            )
            if resp.status_code != 200:
                raise DirectoryUnavailable(f"token endpoint status {resp.status_code}")
            body = resp.json()
            token = str(body["access_token"])
            self._cache[tenant_id] = (time.time() + int(body.get("expires_in", 3600)), token)
            return token


def secrets_manager_loader(secret_arn: str, region: str) -> Any:
    async def load() -> str:
        import boto3

        client = boto3.client("secretsmanager", region_name=region)
        value = await asyncio.to_thread(client.get_secret_value, SecretId=secret_arn)
        raw = value["SecretString"]
        try:
            return str(json.loads(raw)["client_secret"])
        except (ValueError, KeyError):
            return str(raw)

    return load


def graph_directory_from_settings(settings: Settings, http: httpx.AsyncClient) -> DirectoryAdapter:
    if not settings.graph_client_id or not settings.graph_client_secret_arn:
        return _UnavailableDirectory()
    tokens = ClientCredentialTokens(
        http,
        settings.graph_client_id,
        secrets_manager_loader(settings.graph_client_secret_arn, settings.aws_region),
    )
    return GraphDirectory(http, tokens)


class _UnavailableDirectory:
    """No Graph credentials configured: overage users fail closed (HTTP 503)."""

    async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]:
        raise DirectoryUnavailable("Graph directory not configured")
