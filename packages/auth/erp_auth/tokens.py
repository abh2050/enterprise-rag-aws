"""Generic access-token validation shared by the Entra and development providers.

Both providers run exactly this code path; the development provider differs only in issuer and key
source. There is no code path that accepts an unverified token.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import jwt
from jwt import PyJWK

from erp_auth.models import VerifiedIdentity


class TokenValidationError(Exception):
    """Raised for any token that must be rejected. ``code`` is safe to log; details are not returned."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity: ...


JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class JwksCache:
    """Signing-key cache supporting rotation.

    Unknown ``kid`` triggers a refresh, rate-limited so random kids cannot force a fetch per request.
    """

    def __init__(
        self, fetch: JwksFetcher, ttl_seconds: int = 24 * 3600, min_refresh_seconds: int = 30
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> PyJWK:
        now = time.monotonic()
        if kid in self._keys and now - self._fetched_at < self._ttl:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            stale = now - self._fetched_at >= self._ttl
            may_refresh = now - self._fetched_at >= self._min_refresh
            if (kid not in self._keys and may_refresh) or stale:
                await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise TokenValidationError("unknown_signing_key")
        return key

    async def _refresh(self) -> None:
        try:
            document = await self._fetch()
        except Exception as exc:
            raise TokenValidationError("jwks_unavailable") from exc
        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not kid or entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
                continue
            try:
                keys[kid] = PyJWK(entry, algorithm="RS256")
            except Exception:  # noqa: S112 — skip malformed keys rather than failing the whole set
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()


@dataclass(frozen=True)
class TokenPolicy:
    provider: Literal["entra", "dev"]
    audience: str
    allowed_tenants: frozenset[str]
    issuer_for_tenant: Callable[[str], str]
    required_scopes: frozenset[str] = frozenset()
    accepted_app_roles: frozenset[str] = frozenset()
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 60
    required_version: str | None = "2.0"
    max_lifetime_seconds: int = 24 * 3600
    extra_required_claims: tuple[str, ...] = field(default=("exp", "iat", "nbf", "iss", "aud", "tid", "oid"))


class JwtAccessTokenVerifier:
    def __init__(self, policy: TokenPolicy, keys: JwksCache) -> None:
        if not set(policy.algorithms) <= {"RS256", "RS384", "RS512", "PS256", "ES256"}:
            raise ValueError("only asymmetric algorithms may be configured")
        self._policy = policy
        self._keys = keys

    async def verify(self, token: str) -> VerifiedIdentity:
        p = self._policy
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenValidationError("malformed") from exc
        alg = header.get("alg")
        if alg not in p.algorithms:
            raise TokenValidationError("algorithm_not_allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenValidationError("missing_kid")
        key = await self._keys.get(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=p.audience,
                leeway=p.leeway_seconds,
                options={"require": list(p.extra_required_claims), "verify_iss": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenValidationError("not_yet_valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenValidationError("wrong_audience") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenValidationError("missing_claim") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenValidationError("bad_signature") from exc
        except jwt.PyJWTError as exc:
            raise TokenValidationError("invalid") from exc

        tid = claims.get("tid")
        if not isinstance(tid, str) or tid not in p.allowed_tenants:
            raise TokenValidationError("tenant_not_allowed")
        if claims.get("iss") != p.issuer_for_tenant(tid):
            raise TokenValidationError("wrong_issuer")
        if p.required_version is not None and claims.get("ver") != p.required_version:
            raise TokenValidationError("wrong_token_version")
        iat, exp = int(claims["iat"]), int(claims["exp"])
        if exp - iat > p.max_lifetime_seconds:
            raise TokenValidationError("lifetime_too_long")
        if iat > time.time() + p.leeway_seconds:
            raise TokenValidationError("issued_in_future")

        scopes = frozenset(str(claims.get("scp", "")).split()) - {""}
        raw_roles = claims.get("roles", [])
        roles = frozenset(str(r) for r in raw_roles) if isinstance(raw_roles, list) else frozenset()
        # An access token for this API must carry delegated scopes or app roles. ID tokens carry
        # neither (and have the client, not the API, as audience), so they fail here as well.
        has_scope = bool(p.required_scopes) and p.required_scopes <= scopes
        has_role = bool(p.accepted_app_roles & roles)
        if not (has_scope or has_role):
            raise TokenValidationError("insufficient_scope")

        overage = _has_group_overage(claims)
        raw_groups = claims.get("groups")
        groups: frozenset[str] | None
        if overage:
            groups = None
        elif isinstance(raw_groups, list):
            groups = frozenset(str(g) for g in raw_groups)
        else:
            groups = frozenset()

        return VerifiedIdentity(
            provider=p.provider,
            issuer=str(claims["iss"]),
            tenant_id=tid,
            object_id=str(claims["oid"]),
            display_name=claims.get("name") or claims.get("preferred_username"),
            scopes=scopes,
            roles=roles,
            groups=groups,
            groups_overage=overage,
            token_id=claims.get("uti") or claims.get("jti"),
            expires_at=datetime.fromtimestamp(exp, tz=UTC),
        )


def _has_group_overage(claims: dict[str, Any]) -> bool:
    names = claims.get("_claim_names")
    if isinstance(names, dict) and "groups" in names:
        return True
    return claims.get("hasgroups") is True
