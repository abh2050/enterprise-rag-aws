"""Production configuration must refuse development auth, fake providers and unsafe settings."""

import pytest

from erp_api.main import create_app
from erp_rag.config import Settings, UnsafeConfigurationError

SAFE = dict(
    environment="prod",
    auth_provider="entra",
    entra_api_client_id="00000000-0000-0000-0000-00000000000a",
    entra_allowed_tenants=["11111111-1111-4111-8111-111111111111"],
    model_provider="bedrock",
    scanner="guardduty",
    dynamodb_endpoint=None,
    opensearch_auth="sigv4",
    opensearch_url="https://vpc-erp.us-west-2.es.amazonaws.com",
    artifact_store="s3",
    artifact_bucket="erp-prod-artifacts",
    cors_origins=["https://erp.example.com"],
)


def test_safe_prod_settings_pass() -> None:
    Settings(**SAFE).assert_safe_for_environment()


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"auth_provider": "dev"}, "development identity provider"),
        ({"model_provider": "fixture"}, "fixture"),
        ({"scanner": "dev_signature"}, "malware scanner"),
        ({"dynamodb_endpoint": "http://localhost:8001"}, "DynamoDB endpoint"),
        ({"opensearch_auth": "none"}, "SigV4"),
        ({"opensearch_verify_certs": False}, "TLS verification"),
        ({"opensearch_url": "http://vpc-erp"}, "not HTTPS"),
        ({"artifact_store": "local"}, "local filesystem"),
        ({"cors_origins": ["*"]}, "wildcard CORS"),
        ({"entra_allowed_tenants": []}, "tenant allowlist"),
    ],
)
@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_unsafe_settings_refused(env: str, override: dict, fragment: str) -> None:  # type: ignore[type-arg]
    settings = Settings(**{**SAFE, "environment": env, **override})
    with pytest.raises(UnsafeConfigurationError, match=fragment):
        settings.assert_safe_for_environment()
    with pytest.raises(UnsafeConfigurationError):
        create_app(settings)  # the API refuses to even construct


def test_local_environment_allows_dev_components() -> None:
    Settings(environment="local").assert_safe_for_environment()


def test_worker_role_does_not_require_identity_provider_but_keeps_other_checks() -> None:
    worker = Settings(
        **{**SAFE, "service_role": "worker", "entra_api_client_id": None, "entra_allowed_tenants": []}
    )
    worker.assert_safe_for_environment()  # no inbound endpoints → no IdP required
    with pytest.raises(UnsafeConfigurationError, match="fixture"):
        Settings(
            **{**SAFE, "service_role": "worker", "model_provider": "fixture"}
        ).assert_safe_for_environment()
    with pytest.raises(UnsafeConfigurationError, match="tenant allowlist"):
        Settings(**{**SAFE, "entra_allowed_tenants": []}).assert_safe_for_environment()  # API still strict
