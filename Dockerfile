# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY sidecar/ ./sidecar/
COPY tools/ ./tools/
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app /app
RUN mkdir -p /app/var && chown app:app /app/var
VOLUME ["/app/var"]

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    DATABASE_URL="sqlite:////app/var/sidecar.db"

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "sidecar.api:app", "--host", "0.0.0.0", "--port", "8000"]
