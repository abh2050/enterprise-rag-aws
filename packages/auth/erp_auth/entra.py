"""Microsoft Entra ID access-token verification (v2.0 tokens).

Verified against https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference
(accessed 2026-09-30): ``aud`` is the API client id for v2 tokens, ``iss`` ends in ``/v2.0`` and embeds the
tenant id, ``scp`` is space separated, ``roles`` is an array, group overage uses ``_claim_names``.

Live status: implemented-not-live-tested (no tenant available).
"""

from __future__ import annotations

from typing import Any

import httpx

from erp_auth.tokens import JwksCache, JwtAccessTokenVerifier, TokenPolicy

ENTRA_AUTHORITY = "https://login.microsoftonline.com"


def entra_issuer(tenant_id: str) -> str:
    return f"{ENTRA_AUTHORITY}/{tenant_id}/v2.0"


def entra_jwks_uri(tenant_id: str) -> str:
    return f"{ENTRA_AUTHORITY}/{tenant_id}/discovery/v2.0/keys"


def build_entra_verifier(
    *,
    api_client_id: str,
    allowed_tenants: frozenset[str],
    required_scopes: frozenset[str],
    accepted_app_roles: frozenset[str],
    http: httpx.AsyncClient,
    jwks_tenant: str | None = None,
) -> JwtAccessTokenVerifier:
    """Build a verifier for a single- or multi-tenant API registration.

    ``jwks_tenant`` defaults to the single allowed tenant; multi-tenant deployments use ``common`` keys
    but still enforce the ``tid`` allowlist and a per-tenant issuer.
    """
    if not allowed_tenants:
        raise ValueError("at least one allowed tenant is required")
    key_tenant = jwks_tenant or (next(iter(allowed_tenants)) if len(allowed_tenants) == 1 else "common")

    async def fetch() -> dict[str, Any]:
        response = await http.get(entra_jwks_uri(key_tenant), timeout=5.0)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    policy = TokenPolicy(
        provider="entra",
        audience=api_client_id,
        allowed_tenants=allowed_tenants,
        issuer_for_tenant=entra_issuer,
        required_scopes=required_scopes,
        accepted_app_roles=accepted_app_roles,
    )
    return JwtAccessTokenVerifier(policy, JwksCache(fetch))
