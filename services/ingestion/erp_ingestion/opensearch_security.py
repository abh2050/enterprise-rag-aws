"""OpenSearch fine-grained access control (FGAC) bootstrap.

With FGAC + IAM master on Amazon OpenSearch Service, an IAM role that is not the master must be mapped
to an internal OpenSearch role, or every request returns 403. The worker (FGAC master) creates a
least-privilege role for the API and maps the API task role to it. Idempotent (PUT); safe to run on
every worker start.
"""

from __future__ import annotations

from typing import Any

API_ROLE = "erp_api"


def api_role_definition(index_pattern: str) -> dict[str, Any]:
    return {
        "cluster_permissions": [
            "cluster:monitor/health",
            "cluster:monitor/main",
            "indices:data/read/mget",
            "indices:data/read/msearch",
        ],
        "index_permissions": [
            {
                "index_patterns": [index_pattern],
                "allowed_actions": [
                    "read",  # BM25 + k-NN search, get, count
                    "indices:admin/get",
                    "indices:admin/exists",
                    "indices:admin/mappings/get",
                    "indices:admin/refresh",
                    # Admin revoke/tombstone propagate to the index copy (authoritative state is DynamoDB):
                    "indices:data/write/update/byquery",
                    "indices:data/write/delete/byquery",
                    "indices:data/write/update",
                    "indices:data/write/delete",
                    "indices:data/write/bulk*",
                ],
            }
        ],
    }


async def ensure_api_role_mapping(client: Any, *, api_role_arn: str, index_pattern: str) -> None:
    await client.transport.perform_request(
        "PUT", f"/_plugins/_security/api/roles/{API_ROLE}", body=api_role_definition(index_pattern)
    )
    await client.transport.perform_request(
        "PUT", f"/_plugins/_security/api/rolesmapping/{API_ROLE}", body={"backend_roles": [api_role_arn]}
    )
