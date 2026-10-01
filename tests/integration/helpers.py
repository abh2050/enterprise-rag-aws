from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from erp_auth.models import AuthzContext, SensitivityLabel
from erp_connectors.localfs import LocalFileConnector, ManualGovernanceAdapter
from erp_ingestion.pipeline import IngestionPipeline
from erp_rag import ids
from tests.conftest import ACME

ALICE = AuthzContext(
    tenant_id=ACME,
    user_id="a1a1a1a1-0000-4000-8000-000000000001",
    principals=frozenset(
        {"user:a1a1a1a1-0000-4000-8000-000000000001", "group:acme-all-staff", "group:acme-finance"}
    ),
    clearance=SensitivityLabel.CONFIDENTIAL,
    projects=frozenset({"atlas"}),
)
BOB = AuthzContext(
    tenant_id=ACME,
    user_id="b2b2b2b2-0000-4000-8000-000000000002",
    principals=frozenset(
        {"user:b2b2b2b2-0000-4000-8000-000000000002", "group:acme-all-staff", "group:acme-engineering"}
    ),
    clearance=SensitivityLabel.INTERNAL,
)


def access(groups: list[str], label: str = "confidential", **extra: str) -> str:
    lines = [f"tenant_id: {ACME}", f"allowed_groups: [{', '.join(groups)}]", f"sensitivity_label: {label}"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


@contextmanager
def temp_source(pipeline: IngestionPipeline, root: Path) -> Iterator[tuple[str, Path]]:
    name = f"localfs-t{secrets.token_hex(3)}"
    connector = LocalFileConnector(root, name=name)
    pipeline.connectors[name] = connector
    pipeline.governance[name] = ManualGovernanceAdapter(connector)
    try:
        yield name, root
    finally:
        pipeline.connectors.pop(name, None)
        pipeline.governance.pop(name, None)


def doc_id_for(source: str, key: str) -> str:
    return ids.document_id(ACME, source, key)


def unique_term() -> str:
    """A nonsense token so each test's documents are retrieved independently of the shared corpus."""
    return "zq" + secrets.token_hex(4)
