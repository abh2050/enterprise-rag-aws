"""Access-token validation attacks. Tokens are built locally in Entra v2 format (no live tenant)."""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from erp_auth.entra import entra_issuer
from erp_auth.tokens import JwksCache, JwtAccessTokenVerifier, TokenPolicy, TokenValidationError

TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "99999999-9999-4999-8999-999999999999"
API = "api-client-id"


class KeySet:
    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.fetches = 0

    def add(self, kid: str) -> rsa.RSAPrivateKey:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return self.keys[kid]

    async def jwks(self) -> dict[str, Any]:
        self.fetches += 1
        out = []
        for kid, key in self.keys.items():
            jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            jwk.update(kid=kid, use="sig", kty="RSA")
            out.append(jwk)
        return {"keys": out}


@pytest.fixture
def keyset() -> KeySet:
    ks = KeySet()
    ks.add("k1")
    return ks


@pytest.fixture
def verifier(keyset: KeySet) -> JwtAccessTokenVerifier:
    policy = TokenPolicy(
        provider="entra",
        audience=API,
        allowed_tenants=frozenset({TENANT}),
        issuer_for_tenant=entra_issuer,
        required_scopes=frozenset({"access_as_user"}),
    )
    return JwtAccessTokenVerifier(policy, JwksCache(keyset.jwks, min_refresh_seconds=0))


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": entra_issuer(TENANT),
        "aud": API,
        "tid": TENANT,
        "oid": "user-oid",
        "sub": "pairwise",
        "scp": "access_as_user",
        "ver": "2.0",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "groups": ["g1"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign(ks: KeySet, c: dict[str, Any], kid: str = "k1", alg: str = "RS256") -> str:
    return jwt.encode(c, ks.keys[kid], algorithm=alg, headers={"kid": kid})


async def test_valid_token(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    ident = await verifier.verify(sign(keyset, claims()))
    assert ident.tenant_id == TENANT and ident.object_id == "user-oid" and ident.groups == {"g1"}


async def reject(verifier: JwtAccessTokenVerifier, token: str, code: str) -> None:
    with pytest.raises(TokenValidationError) as exc:
        await verifier.verify(token)
    assert exc.value.code == code


async def test_alg_none_rejected(verifier: JwtAccessTokenVerifier) -> None:
    token = jwt.encode(claims(), key=None, algorithm="none", headers={"kid": "k1"})  # type: ignore[arg-type]
    await reject(verifier, token, "algorithm_not_allowed")


async def test_hs256_key_confusion_rejected(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    jwk = await keyset.jwks()
    token = jwt.encode(claims(), key=str(jwk).encode() * 2, algorithm="HS256", headers={"kid": "k1"})
    await reject(verifier, token, "algorithm_not_allowed")


async def test_bad_signature(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    attacker = KeySet()
    attacker.add("k1")
    await reject(verifier, sign(attacker, claims()), "bad_signature")


async def test_wrong_audience(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    await reject(verifier, sign(keyset, claims(aud="some-other-api")), "wrong_audience")


async def test_id_token_is_not_an_access_token(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    # ID tokens: audience is the *client* app and they carry no scp/roles.
    id_token = claims(aud="spa-client-id", scp=None, nonce="n")
    await reject(verifier, sign(keyset, id_token), "wrong_audience")
    # Even if an ID token's audience matched, missing scopes/roles is rejected.
    await reject(verifier, sign(keyset, claims(scp=None)), "insufficient_scope")


async def test_wrong_tenant_and_issuer(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    await reject(
        verifier, sign(keyset, claims(tid=OTHER_TENANT, iss=entra_issuer(OTHER_TENANT))), "tenant_not_allowed"
    )
    await reject(verifier, sign(keyset, claims(iss=entra_issuer(OTHER_TENANT))), "wrong_issuer")
    await reject(
        verifier, sign(keyset, claims(iss="https://sts.windows.net/" + TENANT + "/")), "wrong_issuer"
    )


async def test_time_claims(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    now = int(time.time())
    await reject(verifier, sign(keyset, claims(exp=now - 3600, iat=now - 7200, nbf=now - 7200)), "expired")
    await reject(verifier, sign(keyset, claims(nbf=now + 3600)), "not_yet_valid")
    await reject(verifier, sign(keyset, claims(exp=now + 3 * 86400)), "lifetime_too_long")
    await reject(verifier, sign(keyset, claims(exp=None)), "missing_claim")


async def test_v1_tokens_and_missing_scope(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    await reject(verifier, sign(keyset, claims(ver="1.0")), "wrong_token_version")
    await reject(verifier, sign(keyset, claims(scp="User.Read")), "insufficient_scope")


async def test_app_role_accepted_when_configured(keyset: KeySet) -> None:
    policy = TokenPolicy(
        provider="entra",
        audience=API,
        allowed_tenants=frozenset({TENANT}),
        issuer_for_tenant=entra_issuer,
        accepted_app_roles=frozenset({"Documents.Read.All"}),
    )
    v = JwtAccessTokenVerifier(policy, JwksCache(keyset.jwks, min_refresh_seconds=0))
    ident = await v.verify(sign(keyset, claims(scp=None, roles=["Documents.Read.All"])))
    assert "Documents.Read.All" in ident.roles


async def test_signing_key_rotation(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    await verifier.verify(sign(keyset, claims()))
    keyset.add("k2")  # new key published after the cache was filled
    ident = await verifier.verify(sign(keyset, claims(), kid="k2"))
    assert ident.object_id == "user-oid"
    stranger = KeySet()
    stranger.add("k3")
    await reject(verifier, sign(stranger, claims(), kid="k3"), "unknown_signing_key")


async def test_unknown_kid_refresh_is_rate_limited(keyset: KeySet) -> None:
    policy = TokenPolicy(
        provider="entra",
        audience=API,
        allowed_tenants=frozenset({TENANT}),
        issuer_for_tenant=entra_issuer,
        required_scopes=frozenset({"access_as_user"}),
    )
    v = JwtAccessTokenVerifier(policy, JwksCache(keyset.jwks, min_refresh_seconds=60))
    await v.verify(sign(keyset, claims()))
    fetches = keyset.fetches
    other = KeySet()
    other.add("random-kid")
    for _ in range(5):
        with pytest.raises(TokenValidationError):
            await v.verify(sign(other, claims(), kid="random-kid"))
    assert keyset.fetches == fetches  # attacker-chosen kids do not force JWKS refetches


async def test_group_overage_marks_groups_unknown(verifier: JwtAccessTokenVerifier, keyset: KeySet) -> None:
    c = claims(groups=None)
    c["_claim_names"] = {"groups": "src1"}
    c["_claim_sources"] = {"src1": {"endpoint": "https://graph.windows.net/..."}}
    ident = await verifier.verify(sign(keyset, c))
    assert ident.groups_overage and ident.groups is None
