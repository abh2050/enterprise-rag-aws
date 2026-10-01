"""Directory adapters resolve group membership when the token cannot carry it (group overage).

* ``SyntheticDirectory``: local development users from ``config/dev-directory.yaml``.
* ``GraphDirectory``: Microsoft Graph ``GET /users/{id}/transitiveMemberOf/microsoft.graph.group``
  with application permission ``User.Read.All`` (least privileged per Graph docs, accessed 2026-09-30).
  OData cast requires ``ConsistencyLevel: eventual`` and ``$count=true``.
  Live status: implemented-not-live-tested.

Failures raise ``DirectoryUnavailable``; callers must fail closed (never treat as "all groups").
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx

from erp_auth.devidp import DevDirectoryFile


class DirectoryUnavailable(Exception):
    pass


class DirectoryAdapter(Protocol):
    async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]: ...


class SyntheticDirectory:
    def __init__(self, directory: DevDirectoryFile) -> None:
        self._groups = {(u.tenant_id, u.object_id): frozenset(u.groups) for u in directory.users}

    async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]:
        try:
            return self._groups[(tenant_id, object_id)]
        except KeyError as exc:
            raise DirectoryUnavailable("unknown user") from exc


AppTokenProvider = Callable[[str], Awaitable[str]]  # tenant_id -> Graph app access token


class GraphDirectory:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        http: httpx.AsyncClient,
        app_token: AppTokenProvider,
        *,
        cache_ttl_seconds: int = 300,
        max_pages: int = 50,
    ) -> None:
        self._http = http
        self._app_token = app_token
        self._ttl = cache_ttl_seconds
        self._max_pages = max_pages
        self._cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}

    async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]:
        key = (tenant_id, object_id)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        groups = await self._fetch(tenant_id, object_id)
        self._cache[key] = (time.monotonic(), groups)
        return groups

    async def _fetch(self, tenant_id: str, object_id: str) -> frozenset[str]:
        try:
            token = await self._app_token(tenant_id)
        except Exception as exc:
            raise DirectoryUnavailable("graph token unavailable") from exc
        url: str | None = (
            f"{self.GRAPH}/users/{object_id}/transitiveMemberOf/microsoft.graph.group"
            "?$select=id&$top=999&$count=true"
        )
        headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
        groups: set[str] = set()
        pages = 0
        while url:
            pages += 1
            if pages > self._max_pages:
                raise DirectoryUnavailable("membership too large to resolve")
            try:
                response = await self._http.get(url, headers=headers, timeout=10.0)
            except httpx.HTTPError as exc:
                raise DirectoryUnavailable("graph request failed") from exc
            if response.status_code != 200:
                raise DirectoryUnavailable(f"graph status {response.status_code}")
            body = response.json()
            for item in body.get("value", []):
                gid = item.get("id")
                if isinstance(gid, str):
                    groups.add(gid)
            next_link = body.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and next_link.startswith(self.GRAPH) else None
        return frozenset(groups)
