#!/usr/bin/env bash
# Register new task-definition revisions with IMAGE for the api and worker services and roll them.
# ECS deployment circuit breaker (rollback=true) reverts automatically if new tasks fail health checks.
set -euo pipefail
CLUSTER="$1"; IMAGE="$2"
for SERVICE in api worker; do
  TD_ARN=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].taskDefinition' --output text)
  aws ecs describe-task-definition --task-definition "$TD_ARN" --query 'taskDefinition' > /tmp/td.json
  python3 - "$IMAGE" <<'PY'
import json, sys
td = json.load(open("/tmp/td.json"))
for c in td["containerDefinitions"]:
    c["image"] = sys.argv[1]
keep = ["family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes",
        "requiresCompatibilities", "cpu", "memory", "runtimePlatform"]
json.dump({k: td[k] for k in keep if k in td}, open("/tmp/td-new.json", "w"))
PY
  NEW_TD=$(aws ecs register-task-definition --cli-input-json file:///tmp/td-new.json --query 'taskDefinition.taskDefinitionArn' --output text)
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$NEW_TD" >/dev/null
  echo "$SERVICE -> $NEW_TD"
done
aws ecs wait services-stable --cluster "$CLUSTER" --services api worker
echo "services stable"
