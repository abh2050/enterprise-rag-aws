#!/usr/bin/env bash
# Sync a local collection folder (with its _access.yaml) to the source bucket; EventBridge triggers ingestion.
# Usage: scripts/upload-docs.sh data/private/demo s3://erp-dev-source-<account>/demo
set -euo pipefail
SRC="${1:?local folder}"; DEST="${2:?s3 uri}"
test -f "$SRC/_access.yaml" || { echo "refusing: $SRC/_access.yaml missing (documents would be denied)"; exit 1; }
aws s3 sync "$SRC" "$DEST" --sse aws:kms --exclude ".*"
