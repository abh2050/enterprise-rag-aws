"""Access manifest (``_access.yaml``) shared by the local-file and S3 connectors.

The manifest is the *source permission record* for sources that carry no native ACLs (plain files,
S3 objects). Labels in it are manual governance, never Purview.
"""

from __future__ import annotations

from datetime import date

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

ACCESS_FILE = "_access.yaml"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".html", ".htm"}


class FileOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    allowed_users: list[str] | None = None
    allowed_groups: list[str] | None = None
    denied_users: list[str] | None = None
    denied_groups: list[str] | None = None
    projects: list[str] | None = None
    sensitivity_label: str | None = None
    series_id: str | None = None
    effective_date: date | None = None
    legal_hold: bool | None = None
    owner: str | None = None


class AccessFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    allowed_users: list[str] = []
    allowed_groups: list[str] = []
    denied_users: list[str] = []
    denied_groups: list[str] = []
    projects: list[str] = []
    sensitivity_label: str | None = None
    legal_hold: bool = False
    owner: str | None = None
    files: dict[str, FileOverride] = {}


class EffectiveAccess(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_users: list[str]
    allowed_groups: list[str]
    denied_users: list[str]
    denied_groups: list[str]
    projects: list[str]
    sensitivity_label: str | None
    legal_hold: bool
    owner: str | None
    title: str | None
    series_id: str | None
    effective_date: date | None


def effective_access(access: AccessFile, filename: str) -> EffectiveAccess:
    o = access.files.get(filename, FileOverride())

    def pick(value: list[str] | None, default: list[str]) -> list[str]:
        return value if value is not None else default

    return EffectiveAccess(
        allowed_users=pick(o.allowed_users, access.allowed_users),
        allowed_groups=pick(o.allowed_groups, access.allowed_groups),
        denied_users=pick(o.denied_users, access.denied_users),
        denied_groups=pick(o.denied_groups, access.denied_groups),
        projects=pick(o.projects, access.projects),
        sensitivity_label=o.sensitivity_label or access.sensitivity_label,
        legal_hold=o.legal_hold if o.legal_hold is not None else access.legal_hold,
        owner=o.owner or access.owner,
        title=o.title,
        series_id=o.series_id,
        effective_date=o.effective_date,
    )


def parse_access(text: str) -> AccessFile | None:
    """Parse a manifest; invalid manifests return None (callers deny)."""
    try:
        return AccessFile.model_validate(yaml.safe_load(text) or {})
    except (ValidationError, yaml.YAMLError):
        return None
