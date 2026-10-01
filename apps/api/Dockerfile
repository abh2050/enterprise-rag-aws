# syntax=docker/dockerfile:1.7
# API + ingestion worker image (same code, different command). Reproducible: installs from uv.lock.
FROM python:3.12.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra langsmith
COPY packages packages
COPY apps/api apps/api
COPY services services
COPY evals evals
COPY config config
RUN uv sync --frozen --no-dev --extra langsmith

# Debian 13 base + OS security updates at build time (bookworm base had 4 CRITICAL CVEs; see docs/security-scan.md)
FROM python:3.12.13-slim-trixie
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"
CMD ["uvicorn", "erp_api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
