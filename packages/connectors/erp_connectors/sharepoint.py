"""SharePoint / OneDrive connector via Microsoft Graph (application permissions).

APIs verified 2026-09-30 (docs/sources.md):
* ``GET /drives/{drive-id}/root/delta`` — change feed; follow ``@odata.nextLink`` to ``@odata.deltaLink``;
  ``deleted`` facet; HTTP 410 → full resync. App permission ``Files.Read.All`` (or ``Sites.Selected``).
* ``GET /drives/{drive-id}/items/{item-id}/permissions`` — effective sharing permissions
  (including ``inheritedFrom``).
* ``GET /drives/{drive-id}/items/{item-id}/content`` — file bytes.

Permission translation (deny by default):
* ``grantedToV2.user.id`` / ``grantedToIdentitiesV2[].user.id`` → user principal (Entra object id)
* ``grantedToV2.group.id`` → group principal
* ``siteGroup`` / ``siteUser`` without an Entra id, sharing ``link`` (anonymous/organization/users),
  ``application`` grants → **unsupported** → document ACL marked incomplete → denied.
Caveat from Graph docs: for non-owner callers only permissions applying to the caller are returned; the
app identity's visibility must be validated in the target tenant before enabling (live check required).

Live status: implemented-not-live-tested → blocked (no M365 tenant).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from erp_auth.policy_translation import SourcePermissions
from erp_connectors.base import ChangeEvent, ConnectorError, FetchedContent, SourceRef

GRAPH = "https://graph.microsoft.com/v1.0"
AppToken = Callable[[str], Awaitable[str]]


def translate_graph_permissions(entries: list[dict[str, Any]]) -> tuple[set[str], set[str], list[str]]:
    users: set[str] = set()
    groups: set[str] = set()
    unsupported: list[str] = []
    for p in entries:
        roles = set(p.get("roles", []))
        if not roles & {"read", "write", "owner"}:
            continue
        if "link" in p:
            unsupported.append(f"sharing_link:{p['link'].get('scope', 'unknown')}")
            continue
        identities = (
            [p.get("grantedToV2")] if p.get("grantedToV2") else list(p.get("grantedToIdentitiesV2") or [])
        )
        if not identities:
            unsupported.append("grant_without_identity")
            continue
        for ident in identities:
            if not isinstance(ident, dict):
                continue
            if ident.get("user", {}).get("id"):
                users.add(str(ident["user"]["id"]))
            elif ident.get("group", {}).get("id"):
                groups.add(str(ident["group"]["id"]))
            elif "siteGroup" in ident or "siteUser" in ident:
                unsupported.append("sharepoint_site_principal")
            elif "application" in ident:
                unsupported.append("application_grant")
            else:
                unsupported.append("unknown_identity")
    return users, groups, unsupported


class SharePointConnector:
    def __init__(
        self,
        http: httpx.AsyncClient,
        app_token: AppToken,
        *,
        tenant_id: str,
        drive_id: str,
        name: str | None = None,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._http = http
        self._token = app_token
        self.tenant_id = tenant_id
        self.drive_id = drive_id
        self.name = name or f"sharepoint-{drive_id[:12]}"
        self._max = max_bytes

    async def _get(self, url: str) -> httpx.Response:
        token = await self._token(self.tenant_id)
        resp = await self._http.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0, follow_redirects=True
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ConnectorError(f"graph transient status {resp.status_code}")
        return resp

    def _ref(self, item_id: str) -> SourceRef:
        return SourceRef(source=self.name, source_key=item_id, tenant_id=self.tenant_id)

    async def list_changes(self, cursor: dict[str, str]) -> tuple[list[ChangeEvent], dict[str, str]]:
        url: str | None = cursor.get("__delta_link__") or f"{GRAPH}/drives/{quote(self.drive_id)}/root/delta"
        now = datetime.now(UTC)
        latest: dict[str, dict[str, Any]] = {}
        delta_link: str | None = None
        while url:
            resp = await self._get(url)
            if resp.status_code == 410:  # token expired → caller must run full reconciliation
                return [], {}
            if resp.status_code != 200:
                raise ConnectorError(f"delta status {resp.status_code}")
            body = resp.json()
            for item in body.get("value", []):
                if "folder" in item and "deleted" not in item:
                    continue
                latest[item["id"]] = item  # last occurrence wins (per Graph docs)
            url = body.get("@odata.nextLink")
            delta_link = body.get("@odata.deltaLink", delta_link)
        events: list[ChangeEvent] = []
        for item_id, item in latest.items():
            ref = self._ref(item_id)
            if "deleted" in item:
                events.append(ChangeEvent(kind="delete", ref=ref, source_revision="deleted", detected_at=now))
            else:
                rev = str(item.get("cTag") or item.get("eTag") or item.get("lastModifiedDateTime"))
                events.append(ChangeEvent(kind="upsert", ref=ref, source_revision=rev, detected_at=now))
        new_cursor = {"__delta_link__": delta_link} if delta_link else {}
        return events, new_cursor

    async def list_all(self) -> list[SourceRef]:
        events, _ = await self.list_changes({})
        return [e.ref for e in events if e.kind != "delete"]

    async def fetch(self, ref: SourceRef) -> FetchedContent:
        meta = await self._get(f"{GRAPH}/drives/{quote(self.drive_id)}/items/{quote(ref.source_key)}")
        if meta.status_code != 200:
            raise ConnectorError(f"item status {meta.status_code}")
        item = meta.json()
        if int(item.get("size", 0)) > self._max:
            raise ConnectorError("item exceeds maximum size")
        content = await self._get(
            f"{GRAPH}/drives/{quote(self.drive_id)}/items/{quote(ref.source_key)}/content"
        )
        if content.status_code != 200:
            raise ConnectorError(f"content status {content.status_code}")
        modified = item.get("lastModifiedDateTime")
        return FetchedContent(
            ref=ref,
            filename=item.get("name", "file"),
            data=content.content,
            source_revision=str(item.get("cTag") or item.get("eTag")),
            source_modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            source_uri=item.get("webUrl", f"sharepoint://{self.drive_id}/{ref.source_key}"),
            title=item.get("name", "file").rsplit(".", 1)[0],
            owner=(item.get("createdBy", {}).get("user", {}) or {}).get("id"),
        )

    async def get_permissions(self, ref: SourceRef) -> SourcePermissions:
        url: str | None = f"{GRAPH}/drives/{quote(self.drive_id)}/items/{quote(ref.source_key)}/permissions"
        entries: list[dict[str, Any]] = []
        while url:
            resp = await self._get(url)
            if resp.status_code != 200:
                raise ConnectorError(f"permissions status {resp.status_code}")
            body = resp.json()
            entries.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
        users, groups, unsupported = translate_graph_permissions(entries)
        material = "|".join([*sorted(users), "#", *sorted(groups), "#", *sorted(unsupported)])
        revision = hashlib.sha256(material.encode()).hexdigest()[:16]  # stable across processes
        return SourcePermissions(
            tenant_id=self.tenant_id,
            allowed_users=sorted(users),
            allowed_groups=sorted(groups),
            unsupported_grants=unsupported,
            source_acl_revision=revision,
            retrieved_at=datetime.now(UTC),
        )
