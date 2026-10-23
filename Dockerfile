# syntax=docker/dockerfile:1.7

# ---- builder: resolve dependencies from the lockfile into a virtualenv ----
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
# Copy only dependency manifests first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime: no compilers, no uv, non-root user ----
FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY artifacts ./artifacts

USER app
EXPOSE 8000

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
