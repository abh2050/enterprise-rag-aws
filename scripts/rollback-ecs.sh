#!/usr/bin/env bash
# Roll api and worker back to a previous task-definition revision (e.g. erp-dev-api:41).
set -euo pipefail
CLUSTER="$1"; API_TD="$2"; WORKER_TD="$3"
aws ecs update-service --cluster "$CLUSTER" --service api --task-definition "$API_TD" >/dev/null
aws ecs update-service --cluster "$CLUSTER" --service worker --task-definition "$WORKER_TD" >/dev/null
aws ecs wait services-stable --cluster "$CLUSTER" --services api worker
echo "rolled back"
