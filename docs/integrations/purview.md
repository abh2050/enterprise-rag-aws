# Microsoft Purview governance adapter

**Status:** implemented-not-live-tested → **blocked** (no tenant / Purview account). Contract tests use
documented response shapes (`tests/unit/test_enterprise_adapters.py`). Local and demo data use **manual
governance** (`governance_source=manual`), which is never presented as Purview.

## What Purview is (and is not) in this system
* It **supplies** classification and sensitivity-label metadata, which is mapped into `sensitivity_label`.
* It is **not** the document repository and **not** a source of effective permissions, and it does not grant access.
  Source permissions come from the source system (Graph item permissions, `_access.yaml`), and the two are
  stored as separate fields (`DocPermissionRecord.allowed_principals` vs `sensitivity_label`).

## Supported source capabilities (no parity assumed)
| Source | Mechanism | Output | Verified doc |
|---|---|---|---|
| SharePoint/OneDrive items | Graph v1.0 `POST /drives/{id}/items/{id}/extractSensitivityLabels` (app `Files.Read.All`) | label GUIDs → mapped via `config/governance-map.v1.yaml` | driveitem-extractsensitivitylabels |
| Amazon S3 objects | Purview Data Map multicloud scanning connector, then Atlas `GET /datamap/api/atlas/v2/entity/uniqueAttribute/type/{type}` (api-version 2023-09-01, scope `https://purview.azure.net/.default`) | classification type names → mapped label | register-scan-amazon-s3, datamapdataplane get-by-unique-attributes |

**Not used:** the org-wide label catalog `GET /security/informationProtection/sensitivityLabels` is Graph
**beta only** ("not supported in production"). Label GUID mappings are therefore maintained in the versioned mapping file.

## Required tenant setup
* Purview Information Protection labels published, plus the label GUIDs copied into `governance-map.v1.yaml` (replacing the placeholders).
* For S3: a Purview account, the Multicloud Scanning Connector add-on, and an AWS IAM role for the Purview scanner (external ID,
  `GetBucketLocation`, `GetBucketPublicAccessBlock`, `GetObject`, `ListBucket`, and `kms:Decrypt` for SSE-KMS buckets).
  A Purview credential with that role ARN, and a scan on the source bucket.
* App registration with access to the Purview data plane (Data Reader role on the collection). Its secret goes in Secrets Manager.
* **Unverified:** the Atlas `typeName` and `qualifiedName` format for scanned S3 objects (`ERP_PURVIEW_S3_TYPE_NAME`)
  must be read from a real scan before enabling.

## Metadata refresh behaviour
* Labels are fetched at ingestion and on every permission/governance sync. `governance_version` is stamped on the
  record and chunks.
* Purview S3 scans are batch (the docs say results can take up to 24h). Entities older than `max_metadata_age_seconds`
  are treated as **stale → no label → deny**.

## Missing / stale / protected behaviour
| Condition | Result |
|---|---|
| No label / entity not scanned | `mapped_label=None` → `unlabeled` → denied (unless the collection sets a default) |
| Unknown label GUID | denied (never guessed) |
| Stale scan | denied |
| `423 fileDoubleKeyEncrypted` / `fileDecryptionNotSupported` | `protected_content` → quarantined, **never decrypted** |
| `423 fileDecryptionDeferred`, 5xx | transient error, so the event is retried |

## Network and region
* Egress from private subnets to `graph.microsoft.com`, `login.microsoftonline.com`, and
  `<account>.purview.azure.com` only, via NAT plus the Network Firewall domain allowlist (enabled in staging/prod).
* Purview scans a us-west-2 bucket in US West (Oregon). Purview private endpoints are **not supported** for S3 scans.
  Scan results (metadata and classifications) are stored in Azure in the Purview account's region, which is a data-residency
  consideration for the data owner.
