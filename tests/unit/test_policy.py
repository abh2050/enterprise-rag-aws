from datetime import UTC, datetime, timedelta

import pytest

from erp_auth.models import AuthzContext, DenyReason, DocPermissionRecord, PolicySettings, SensitivityLabel
from erp_auth.policy import decide, search_filter
from erp_auth.policy_translation import GovernanceMetadata, SourcePermissions, translate

NOW = datetime(2026, 9, 30, tzinfo=UTC)
SETTINGS = PolicySettings(max_acl_age_seconds=3600)
CTX = AuthzContext(
    tenant_id="t1",
    user_id="u1",
    principals=frozenset({"user:u1", "group:fin"}),
    clearance=SensitivityLabel.CONFIDENTIAL,
    projects=frozenset({"atlas"}),
)


def rec(**kw) -> DocPermissionRecord:  # type: ignore[no-untyped-def]
    base = dict(
        tenant_id="t1",
        document_id="d1",
        current_version="v1",
        status="published",
        allowed_principals=frozenset({"group:fin"}),
        sensitivity_label="internal",
        acl_version=1,
        acl_synced_at=NOW - timedelta(minutes=5),
        acl_complete=True,
    )
    base.update(kw)
    return DocPermissionRecord(**base)


def test_allows_matching_group() -> None:
    assert decide(CTX, rec(), settings=SETTINGS, now=NOW).allowed


@pytest.mark.parametrize(
    ("record", "version", "reason"),
    [
        (None, None, DenyReason.RECORD_MISSING),
        (rec(tenant_id="t2"), None, DenyReason.TENANT_MISMATCH),
        (rec(revoked=True), None, DenyReason.REVOKED),
        (rec(status="deleted"), None, DenyReason.NOT_PUBLISHED),
        (rec(status="quarantined"), None, DenyReason.NOT_PUBLISHED),
        (rec(current_version=None), None, DenyReason.NOT_PUBLISHED),
        (rec(), "v0", DenyReason.VERSION_NOT_CURRENT),
        (rec(acl_complete=False), None, DenyReason.ACL_INCOMPLETE),
        (rec(acl_synced_at=NOW - timedelta(hours=2)), None, DenyReason.ACL_STALE),
        (rec(acl_synced_at=None), None, DenyReason.ACL_STALE),
        (rec(denied_principals=frozenset({"user:u1"})), None, DenyReason.EXPLICIT_DENY),
        (rec(allowed_principals=frozenset({"group:eng"})), None, DenyReason.NO_GRANT),
        (rec(allowed_principals=frozenset()), None, DenyReason.NO_GRANT),
        (rec(project_ids=frozenset({"falcon"})), None, DenyReason.PROJECT_RESTRICTED),
        (rec(sensitivity_label="secret-ish"), None, DenyReason.LABEL_UNKNOWN),
        (rec(sensitivity_label="unlabeled"), None, DenyReason.LABEL_UNKNOWN),
        (rec(sensitivity_label="restricted"), None, DenyReason.LABEL_ABOVE_CLEARANCE),
    ],
)
def test_deny_reasons(record, version, reason) -> None:  # type: ignore[no-untyped-def]
    decision = decide(CTX, record, settings=SETTINGS, version=version, now=NOW)
    assert not decision.allowed
    assert decision.reason == reason


def test_explicit_deny_wins_over_allow() -> None:
    record = rec(
        allowed_principals=frozenset({"group:fin", "user:u1"}), denied_principals=frozenset({"group:fin"})
    )
    assert decide(CTX, record, settings=SETTINGS, now=NOW).reason == DenyReason.EXPLICIT_DENY


def test_project_member_allowed() -> None:
    assert decide(CTX, rec(project_ids=frozenset({"atlas"})), settings=SETTINGS, now=NOW).allowed


def test_search_filter_is_built_only_from_context() -> None:
    f = search_filter(CTX)["bool"]
    flat = str(f)
    assert {"term": {"tenant_id": "t1"}} in f["filter"]
    assert {"term": {"publication_status": "published"}} in f["filter"]
    assert {"terms": {"allowed_principals": ["group:fin", "user:u1"]}} in f["filter"]
    assert {"terms": {"sensitivity_label": ["public", "internal", "confidential"]}} in f["filter"]
    assert f["must_not"] == [{"terms": {"denied_principals": ["group:fin", "user:u1"]}}]
    assert "'restricted'" not in flat  # the label value, not the project_restricted field


def test_translation_keeps_permissions_and_classification_separate() -> None:
    perms = SourcePermissions(
        tenant_id="t1", allowed_groups=["fin"], source_acl_revision="r1", retrieved_at=NOW
    )
    gov = GovernanceMetadata(
        source="purview", raw_label_id="guid-123", mapped_label="confidential", governance_version="g1"
    )
    record = translate(
        document_id="d1",
        permissions=perms,
        governance=gov,
        status="published",
        current_version="v1",
        acl_version=1,
        title="t",
        source_uri="u",
        source_modified_at=None,
    )
    assert record.allowed_principals == {"group:fin"}  # label never grants access
    assert record.sensitivity_label == "confidential" and record.governance_source == "purview"
    assert record.acl_complete


def test_translation_unsupported_grants_and_missing_label_deny() -> None:
    perms = SourcePermissions(
        tenant_id="t1",
        allowed_groups=["fin"],
        unsupported_grants=["sharing-link:org"],
        source_acl_revision="r1",
        retrieved_at=NOW,
    )
    record = translate(
        document_id="d1",
        permissions=perms,
        governance=GovernanceMetadata(source="none"),
        status="published",
        current_version="v1",
        acl_version=1,
        title="t",
        source_uri="u",
        source_modified_at=None,
    )
    assert not record.acl_complete
    assert record.sensitivity_label == "unlabeled"
    assert not decide(CTX, record, settings=SETTINGS).allowed
