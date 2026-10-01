"""CONTRACT tests for enterprise adapters using documented response shapes (no live tenant).

These verify request construction and response translation; they are NOT live verification.
Live checks for each adapter are listed in docs/production-readiness.md and remain BLOCKED.
"""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime

import boto3
import httpx
import jwt
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from erp_auth.context import ContextBuilder, EntitlementMap, RoleEntitlement
from erp_auth.directory import DirectoryUnavailable, GraphDirectory
from erp_auth.entra import build_entra_verifier, entra_issuer
from erp_auth.models import SensitivityLabel, VerifiedIdentity
from erp_connectors.base import SourceRef
from erp_connectors.purview import GovernanceMapping, GraphSensitivityLabelAdapter, PurviewDataMapGovernance
from erp_connectors.s3 import S3Connector
from erp_connectors.sharepoint import SharePointConnector, translate_graph_permissions
from erp_rag.config import REPO_ROOT

TENANT = "11111111-1111-4111-8111-111111111111"
MAPPING = GovernanceMapping.load(REPO_ROOT / "config" / "governance-map.v1.yaml")


async def token(_tenant: str) -> str:
    return "app-token"


# ---------------------------------------------------------------- Entra JWKS endpoint + Graph overage


async def test_entra_verifier_fetches_tenant_jwks() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "kid1", "use": "sig"}
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        v = build_entra_verifier(
            api_client_id="api-id",
            allowed_tenants=frozenset({TENANT}),
            required_scopes=frozenset({"access_as_user"}),
            accepted_app_roles=frozenset(),
            http=http,
        )
        now = int(time.time())
        tok = jwt.encode(
            {
                "iss": entra_issuer(TENANT),
                "aud": "api-id",
                "tid": TENANT,
                "oid": "o",
                "scp": "access_as_user",
                "ver": "2.0",
                "iat": now,
                "nbf": now,
                "exp": now + 600,
            },
            key,
            algorithm="RS256",
            headers={"kid": "kid1"},
        )
        ident = await v.verify(tok)
    assert ident.provider == "entra"
    assert seen == [f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"]


async def test_graph_overage_pages_and_fails_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["ConsistencyLevel"] == "eventual"
        if "skiptoken" in str(req.url):
            return httpx.Response(200, json={"value": [{"id": "g3"}]})
        assert "/users/oid-1/transitiveMemberOf/microsoft.graph.group" in str(req.url)
        return httpx.Response(
            200,
            json={
                "value": [{"id": "g1"}, {"id": "g2"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skiptoken=abc",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await GraphDirectory(http, token).groups_for(TENANT, "oid-1") == {"g1", "g2", "g3"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        with pytest.raises(DirectoryUnavailable):
            await GraphDirectory(http, token).groups_for(TENANT, "oid-1")


async def test_context_builder_uses_directory_on_overage_and_roles_for_clearance() -> None:
    class Dir:
        async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]:
            return frozenset({"from-directory"})

    ents = EntitlementMap(
        version="t",
        roles={
            "Clearance.Confidential": RoleEntitlement(clearance=SensitivityLabel.CONFIDENTIAL),
            "Project.X": RoleEntitlement(projects=["x"]),
        },
    )
    ident = VerifiedIdentity(
        provider="entra",
        issuer="i",
        tenant_id=TENANT,
        object_id="u",
        groups=None,
        groups_overage=True,
        roles=frozenset({"Clearance.Confidential", "Project.X", "Unknown"}),
        expires_at=datetime.now(UTC),
    )
    ctx = await ContextBuilder(Dir(), ents).build(ident)
    assert ctx.principals == {"user:u", "group:from-directory"}
    assert ctx.clearance == SensitivityLabel.CONFIDENTIAL and ctx.projects == {"x"} and not ctx.is_admin

    class Down:
        async def groups_for(self, tenant_id: str, object_id: str) -> frozenset[str]:
            raise DirectoryUnavailable("down")

    with pytest.raises(DirectoryUnavailable):  # never silently treated as "no groups" or "all groups"
        await ContextBuilder(Down(), ents).build(ident)


# ---------------------------------------------------------------- SharePoint


def test_graph_permission_translation_denies_ambiguous_grants() -> None:
    users, groups, unsupported = translate_graph_permissions(
        [
            {"roles": ["read"], "grantedToV2": {"user": {"id": "u1", "displayName": "A"}}},
            {"roles": ["write"], "grantedToV2": {"group": {"id": "g1"}}, "inheritedFrom": {"id": "parent"}},
            {
                "roles": ["read"],
                "grantedToIdentitiesV2": [{"user": {"id": "u2"}}, {"siteGroup": {"id": "3"}}],
            },
            {"roles": ["write"], "link": {"type": "edit", "scope": "organization"}},
            {"roles": ["owner"], "grantedToV2": {"application": {"id": "app"}}},
            {"roles": ["sp.limited"], "grantedToV2": {"user": {"id": "ignored"}}},
        ]
    )
    assert users == {"u1", "u2"} and groups == {"g1"}
    assert sorted(unsupported) == [
        "application_grant",
        "sharepoint_site_principal",
        "sharing_link:organization",
    ]


async def test_sharepoint_delta_fetch_and_permissions() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        assert req.headers["Authorization"] == "Bearer app-token"
        if url.endswith("/root/delta"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f1", "name": "folder", "folder": {}},
                        {"id": "i1", "name": "a.docx", "file": {}, "cTag": "c1"},
                        {"id": "i2", "name": "b.pdf", "deleted": {}},
                    ],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=p2",
                },
            )
        if "token=p2" in url:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "i1", "name": "a.docx", "file": {}, "cTag": "c2"}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/d1/root/delta?token=next",
                },
            )
        if url.endswith("/items/i1/permissions"):
            return httpx.Response(
                200, json={"value": [{"roles": ["read"], "grantedToV2": {"group": {"id": "g1"}}}]}
            )
        if url.endswith("/items/i1/content"):
            return httpx.Response(200, content=b"%PDF-1.7 bytes")
        if url.endswith("/items/i1"):
            return httpx.Response(
                200,
                json={
                    "id": "i1",
                    "name": "a.docx",
                    "cTag": "c2",
                    "size": 10,
                    "lastModifiedDateTime": "2026-01-02T03:04:05Z",
                    "webUrl": "https://x/a.docx",
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sp = SharePointConnector(http, token, tenant_id=TENANT, drive_id="d1", name="sp")
        events, cursor = await sp.list_changes({})
        assert {(e.kind, e.ref.source_key, e.source_revision) for e in events} == {
            ("upsert", "i1", "c2"),
            ("delete", "i2", "deleted"),
        }
        assert cursor["__delta_link__"].endswith("token=next")
        perms = await sp.get_permissions(SourceRef(source="sp", source_key="i1", tenant_id=TENANT))
        assert perms.allowed_groups == ["g1"] and not perms.unsupported_grants
        content = await sp.fetch(SourceRef(source="sp", source_key="i1", tenant_id=TENANT))
        assert content.source_uri == "https://x/a.docx" and content.source_revision == "c2"


# ---------------------------------------------------------------- Purview


async def test_purview_graph_labels_mapping_and_protected_content() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "value": {
                        "labels": [
                            {
                                "sensitivityLabelId": "00000000-0000-0000-0000-00000000a003",
                                "assignmentMethod": "standard",
                                "tenantId": TENANT,
                            }
                        ]
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "value": {
                        "labels": [
                            {
                                "sensitivityLabelId": "ffffffff-unknown",
                                "assignmentMethod": "standard",
                                "tenantId": TENANT,
                            }
                        ]
                    }
                },
            ),
            httpx.Response(423, json={"error": {"code": "fileDoubleKeyEncrypted", "message": "locked"}}),
            httpx.Response(200, json={"value": {"labels": []}}),
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and str(req.url).endswith("/drives/d1/items/i1/extractSensitivityLabels")
        return next(responses)

    ref = SourceRef(source="sp", source_key="i1", tenant_id=TENANT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = GraphSensitivityLabelAdapter(http, token, MAPPING, tenant_id=TENANT, drive_id="d1")
        known = await adapter.get(ref)
        unknown = await adapter.get(ref)
        protected = await adapter.get(ref)
        unlabeled = await adapter.get(ref)
    assert (known.source, known.mapped_label) == ("purview", "confidential")
    assert unknown.mapped_label is None  # unknown label GUID → deny, never guessed
    assert protected.protected_content  # → quarantined, never decrypted
    assert unlabeled.mapped_label is None


async def test_purview_datamap_classifications_stale_and_missing() -> None:
    fresh_ms = int(time.time() * 1000)
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "entity": {
                        "updateTime": fresh_ms,
                        "version": 3,
                        "classifications": [
                            {"typeName": "MICROSOFT.PERSONAL.EMAIL"},
                            {"typeName": "MICROSOFT.FINANCIAL.CREDIT_CARD_NUMBER"},
                        ],
                    }
                },
            ),
            httpx.Response(200, json={"entity": {"updateTime": 1000, "classifications": []}}),
            httpx.Response(404, json={"errorCode": "ATLAS-404"}),
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/datamap/api/atlas/v2/entity/uniqueAttribute/type/aws_s3_object"
        assert req.url.params["attr:qualifiedName"] == "s3://bucket/docs/a.pdf"
        assert req.url.params["api-version"] == "2023-09-01"
        return next(responses)

    ref = SourceRef(source="s3", source_key="docs/a.pdf", tenant_id=TENANT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = PurviewDataMapGovernance(
            http,
            token,
            MAPPING,
            tenant_id=TENANT,
            endpoint="https://acct.purview.azure.com",
            bucket="bucket",
            type_name="aws_s3_object",
        )
        classified = await adapter.get(ref)
        stale = await adapter.get(ref)
        missing = await adapter.get(ref)
    assert classified.mapped_label == "restricted"  # highest mapped classification wins
    assert stale.mapped_label is None and stale.governance_version.endswith(":stale")
    assert missing.source == "none" and missing.mapped_label is None


# ---------------------------------------------------------------- S3


def _body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


async def test_s3_connector_contract() -> None:
    s3 = boto3.client("s3", region_name="us-west-2", aws_access_key_id="x", aws_secret_access_key="x")
    access = f"tenant_id: {TENANT}\nallowed_groups: [g1]\nsensitivity_label: internal\n".encode()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "docs/hr/_access.yaml", "ETag": '"a"', "Size": 1},
                    {"Key": "docs/hr/policy.md", "ETag": '"e1"', "Size": 10},
                    {"Key": "docs/hr/image.png", "ETag": '"e2"', "Size": 10},
                ],
                "IsTruncated": False,
            },
            {"Bucket": "b", "Prefix": "docs/"},
        )
        stub.add_response(
            "get_object", {"Body": _body(access)}, {"Bucket": "b", "Key": "docs/hr/_access.yaml"}
        )
        conn = S3Connector(s3, "b", prefix="docs", name="s3")
        events, cursor = await conn.list_changes({})
        stub.add_response(
            "head_object",
            {
                "ContentLength": 10,
                "ETag": '"e1"',
                "VersionId": "ver1",
                "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {"Bucket": "b", "Key": "docs/hr/policy.md"},
        )
        stub.add_response(
            "get_object",
            {"Body": _body(b"# Policy\n\ntext")},
            {"Bucket": "b", "Key": "docs/hr/policy.md", "VersionId": "ver1"},
        )
        stub.add_response(
            "get_object", {"Body": _body(access)}, {"Bucket": "b", "Key": "docs/hr/_access.yaml"}
        )
        fetched = await conn.fetch(events[0].ref)
        stub.add_response(
            "get_object", {"Body": _body(access)}, {"Bucket": "b", "Key": "docs/hr/_access.yaml"}
        )
        perms = await conn.get_permissions(events[0].ref)
    assert [(e.kind, e.ref.source_key, e.ref.tenant_id) for e in events] == [
        ("upsert", "docs/hr/policy.md", TENANT)
    ]
    assert fetched.source_revision == "ver1" and fetched.source_uri == "s3://b/docs/hr/policy.md"
    assert perms.allowed_groups == ["g1"]
    assert json.dumps(cursor)
