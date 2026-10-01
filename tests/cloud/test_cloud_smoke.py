"""OPT-IN smoke tests against a DEPLOYED environment (never run in CI by default).

Required env: RUN_CLOUD_TESTS=1, ERP_CLOUD_BASE_URL (CloudFront URL),
ERP_CLOUD_TOKEN_ALLOWED and
ERP_CLOUD_TOKEN_DENIED (Entra access tokens for two test users), ERP_CLOUD_QUESTION and
ERP_CLOUD_EXPECTED_TITLE
(a question whose answer is in a document only the allowed user may read).
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = [
    pytest.mark.cloud,
    pytest.mark.skipif(
        os.environ.get("RUN_CLOUD_TESTS") != "1", reason="cloud tests are opt-in (RUN_CLOUD_TESTS=1)"
    ),
]


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    keys = [
        "ERP_CLOUD_BASE_URL",
        "ERP_CLOUD_TOKEN_ALLOWED",
        "ERP_CLOUD_TOKEN_DENIED",
        "ERP_CLOUD_QUESTION",
        "ERP_CLOUD_EXPECTED_TITLE",
    ]
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.fail(f"missing {missing}")
    return {k: os.environ[k] for k in keys}


def test_health_and_security_headers(env: dict[str, str]) -> None:
    r = httpx.get(env["ERP_CLOUD_BASE_URL"] + "/readyz", timeout=10)
    assert r.status_code == 200 and r.json()["checks"] == {"opensearch": "ok", "dynamodb": "ok"}
    page = httpx.get(env["ERP_CLOUD_BASE_URL"] + "/", timeout=10)
    assert "strict-transport-security" in page.headers and "content-security-policy" in page.headers


def test_unauthenticated_rejected(env: dict[str, str]) -> None:
    r = httpx.post(env["ERP_CLOUD_BASE_URL"] + "/api/ask", json={"question": "x"}, timeout=10)
    assert r.status_code == 401


def test_authorization_gate_in_cloud(env: dict[str, str]) -> None:
    base, q = env["ERP_CLOUD_BASE_URL"], env["ERP_CLOUD_QUESTION"]
    allowed = httpx.post(
        base + "/api/ask",
        json={"question": q},
        timeout=90,
        headers={"Authorization": f"Bearer {env['ERP_CLOUD_TOKEN_ALLOWED']}"},
    ).json()
    target = next(c for c in allowed["citations"] if c["title"] == env["ERP_CLOUD_EXPECTED_TITLE"])
    assert allowed["inference_mode"] == "live"
    denied_hdr = {"Authorization": f"Bearer {env['ERP_CLOUD_TOKEN_DENIED']}"}
    denied = httpx.post(base + "/api/ask", json={"question": q}, timeout=90, headers=denied_hdr).json()
    assert all(c["document_id"] != target["document_id"] for c in denied["citations"])
    assert httpx.get(base + target["open_url"], headers=denied_hdr, timeout=10).status_code == 404
