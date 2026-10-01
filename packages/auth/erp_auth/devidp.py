"""Development identity provider for local use only.

Issues RS256 access tokens shaped like Entra v2 tokens for *synthetic* users defined in
``config/dev-directory.yaml``. Tokens are verified by the same ``JwtAccessTokenVerifier`` used for Entra;
there is no bypass. Production startup refuses to run with this provider enabled
(see ``erp_rag.config.Settings.assert_safe_for_environment``).
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import jwt
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, ConfigDict

from erp_auth.tokens import JwksCache, JwtAccessTokenVerifier, TokenPolicy

DEV_SCOPE = "access_as_user"


class DevUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    username: str
    display_name: str
    tenant_id: str
    object_id: str
    groups: list[str]
    roles: list[str] = []
    description: str = ""


class DevTenant(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    name: str


class DevDirectoryFile(BaseModel):
    tenants: list[DevTenant]
    users: list[DevUser]
    overage_threshold: int = 200


def load_dev_directory(path: Path) -> DevDirectoryFile:
    return DevDirectoryFile.model_validate(yaml.safe_load(path.read_text()))


class DevIdentityProvider:
    def __init__(
        self, *, issuer: str, audience: str, directory: DevDirectoryFile, key_path: Path | None = None
    ):
        self.issuer = issuer
        self.audience = audience
        self.directory = directory
        self._key = _load_or_create_key(key_path)
        self.kid = "dev-" + RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)["n"][:16]

    @property
    def users(self) -> list[DevUser]:
        return self.directory.users

    def user(self, username: str) -> DevUser | None:
        return next((u for u in self.directory.users if u.username == username), None)

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def issue_token(self, username: str, *, lifetime_seconds: int = 3600, **overrides: Any) -> str:
        user = self.user(username)
        if user is None:
            raise KeyError(username)
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "tid": user.tenant_id,
            "oid": user.object_id,
            "sub": user.object_id,
            "name": user.display_name,
            "preferred_username": user.username,
            "scp": DEV_SCOPE,
            "roles": user.roles,
            "ver": "2.0",
            "iat": now,
            "nbf": now,
            "exp": now + lifetime_seconds,
            "uti": secrets.token_urlsafe(12),
        }
        if len(user.groups) > self.directory.overage_threshold:
            # Mirror Entra's overage behaviour so the directory adapter path is exercised locally.
            claims["_claim_names"] = {"groups": "src1"}
            claims["_claim_sources"] = {"src1": {"endpoint": "dev-directory"}}
        else:
            claims["groups"] = user.groups
        claims.update(overrides)
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})

    def sign_raw(self, claims: dict[str, Any], *, headers: dict[str, Any] | None = None) -> str:
        """Sign arbitrary claims (tests only — used to build malformed-but-signed tokens)."""
        hdr = {"kid": self.kid, **(headers or {})}
        return jwt.encode(claims, self._key, algorithm="RS256", headers=hdr)

    def verifier(self) -> JwtAccessTokenVerifier:
        async def fetch() -> dict[str, Any]:
            return self.jwks()

        policy = TokenPolicy(
            provider="dev",
            audience=self.audience,
            allowed_tenants=frozenset(t.tenant_id for t in self.directory.tenants),
            issuer_for_tenant=lambda _tid: self.issuer,
            required_scopes=frozenset({DEV_SCOPE}),
        )
        return JwtAccessTokenVerifier(policy, JwksCache(fetch, min_refresh_seconds=0))


def _load_or_create_key(path: Path | None) -> rsa.RSAPrivateKey:
    if path is not None and path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("dev IdP key must be RSA")
        return loaded
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)
    return key


def describe_users(provider: DevIdentityProvider) -> str:
    return json.dumps([u.model_dump() for u in provider.users], indent=2)
