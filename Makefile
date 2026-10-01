# enterprise-rag-platform — developer commands
SHELL := /bin/bash
UV := env -u VIRTUAL_ENV uv run
TF_IMAGE := hashicorp/terraform:1.13.3
TF := docker run --rm -v $(CURDIR)/infra:/infra -w /infra $(TF_IMAGE)

.PHONY: help install up-deps down up dev-api dev-web ingest status test test-unit test-integration test-e2e \
        test-security lint fmt typecheck web-check eval tf-fmt tf-validate check clean live-bedrock

help:
	@grep -E '^[a-z-]+:' Makefile | sed 's/:.*//' | sort | tr '\n' ' '; echo

install:            ## Python + web dependencies from lockfiles
	env -u VIRTUAL_ENV uv sync --frozen --all-extras
	cd apps/web && npm ci --no-audit --no-fund

up-deps:            ## local OpenSearch 3.1 + DynamoDB Local
	docker compose up -d --wait opensearch dynamodb

down:
	docker compose --profile app down

up: up-deps         ## full stack in containers (api + web), then ingest synthetic corpus
	@test -f .env.local || cp .env.local.example .env.local
	docker compose --profile app up -d --build --wait
	docker compose exec api erp-ingest sync --source all || true
	@echo "Web: http://localhost:5173   API: http://localhost:8000/api/docs"

dev-api: up-deps    ## API with reload on the host
	$(UV) uvicorn erp_api.main:app_factory --factory --reload --host 127.0.0.1 --port 8000

dev-web:            ## Vite dev server (proxies /api to :8000)
	cd apps/web && npm run dev

ingest: up-deps     ## ingest data/synthetic and data/private
	$(UV) erp-ingest sync --source all

status:
	$(UV) erp-ingest status --tenant 11111111-1111-4111-8111-111111111111

test-unit:
	$(UV) pytest tests/unit tests/security -m "not integration" -q

test-integration: up-deps
	$(UV) pytest tests/integration -q

test-e2e: up-deps   ## STAGE 1 GATE and API-level security tests
	$(UV) pytest tests/e2e -q

test-security: up-deps
	$(UV) pytest tests/security -q

test: up-deps       ## all deterministic tests (unit + integration + e2e + security); no paid APIs
	$(UV) pytest -q -m "not live_bedrock and not cloud"

live-bedrock:       ## OPT-IN: calls real Bedrock (costs money). Requires AWS credentials.
	RUN_LIVE_BEDROCK=1 $(UV) pytest tests/cloud -m live_bedrock -q

lint:
	$(UV) ruff check .
	$(UV) ruff format --check packages apps services evals tests scripts

fmt:
	$(UV) ruff format packages apps services evals tests scripts
	$(UV) ruff check --fix .

typecheck:
	$(UV) mypy packages apps services evals

web-check:
	cd apps/web && npm run lint && npm run typecheck && npm test && npm run build

eval: up-deps       ## deterministic eval report (FIXTURE inference; not model-quality evidence)
	$(UV) erp-eval --dataset evals/datasets/synthetic_v1 --out evals/reports

tf-fmt:
	$(TF) fmt -recursive -check

tf-validate:        ## terraform init -backend=false + validate for every root (no AWS calls)
	@for root in terraform/envs/dev terraform/envs/staging terraform/envs/prod langsmith; do \
	  echo "== $$root"; \
	  docker run --rm -v $(CURDIR)/infra:/infra -w /infra/$$root $(TF_IMAGE) init -backend=false -input=false >/dev/null && \
	  docker run --rm -v $(CURDIR)/infra:/infra -w /infra/$$root $(TF_IMAGE) validate || exit 1; \
	done

check: lint typecheck test web-check tf-fmt tf-validate

clean:
	rm -rf var .pytest_cache .mypy_cache .ruff_cache apps/web/dist
