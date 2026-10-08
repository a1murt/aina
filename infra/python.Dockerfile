# One image recipe for every Python service of the uv workspace.
#   docker build -f infra/python.Dockerfile --build-arg PACKAGE=qost-api .
# PACKAGE selects the workspace member (qost-sim, qost-collector, qost-engine, qost-api,
# qost-notifier); compose passes it and sets the command (python -m qost_<service>).
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12-slim-bookworm
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PLANT_CONFIG_DIR=/app/config
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
# libgomp1: the OpenMP runtime LightGBM needs (qost-ml, used by the engine's PdM serving)
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /var/lib/qost/spool /app/config \
    && chown -R app /var/lib/qost

# 1) Third-party dependencies: cached while the lockfile and manifests do not change.
COPY pyproject.toml uv.lock ./
COPY packages/twin_core/pyproject.toml packages/twin_core/pyproject.toml
COPY services/sim/pyproject.toml services/sim/pyproject.toml
COPY services/collector/pyproject.toml services/collector/pyproject.toml
COPY services/engine/pyproject.toml services/engine/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/notifier/pyproject.toml services/notifier/pyproject.toml
COPY ml/pyproject.toml ml/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}" \
    && rm -rf /root/.cache/uv

# 2) Workspace sources (config/ is mounted read-only at runtime).
COPY packages packages
COPY services services
COPY ml/src ml/src
# The committed models and the feature texts are part of the image (no volume on top of them):
# the engine serves them offline, the same files the tests check.
COPY ml/models ml/models
COPY ml/feature_catalog.yaml ml/feature_catalog.yaml
RUN uv sync --frozen --no-dev --package "${PACKAGE}" && rm -rf /root/.cache/uv

USER app
