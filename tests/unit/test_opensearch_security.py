from erp_ingestion.opensearch_security import api_role_definition, ensure_api_role_mapping


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def perform_request(self, method: str, url: str, body: dict) -> dict:  # type: ignore[type-arg]
        self.calls.append((method, url, body))
        return {}


class FakeClient:
    def __init__(self) -> None:
        self.transport = FakeTransport()


async def test_api_role_is_scoped_to_the_chunk_index_and_mapped_to_the_role_arn() -> None:
    client = FakeClient()
    await ensure_api_role_mapping(
        client, api_role_arn="arn:aws:iam::1:role/erp-dev-api-x", index_pattern="erp-chunks-*"
    )
    (m1, u1, role), (m2, u2, mapping) = client.transport.calls
    assert (m1, u1) == ("PUT", "/_plugins/_security/api/roles/erp_api")
    assert role["index_permissions"][0]["index_patterns"] == ["erp-chunks-*"]
    assert (
        "indices:admin/delete" not in role["index_permissions"][0]["allowed_actions"]
    )  # cannot drop the index
    assert (m2, u2, mapping) == (
        "PUT",
        "/_plugins/_security/api/rolesmapping/erp_api",
        {"backend_roles": ["arn:aws:iam::1:role/erp-dev-api-x"]},
    )
    assert "*" not in str(api_role_definition("x")["cluster_permissions"])
