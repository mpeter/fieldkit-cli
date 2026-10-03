FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

FROM docker.io/library/python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git jq make libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /opt/fieldkit
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/fieldkit/ ./src/fieldkit/
RUN uv sync --frozen --all-extras --dev --no-cache && \
    rm /usr/local/bin/python && \
    printf '#!/bin/sh\nexec /opt/fieldkit/.venv/bin/python "$@"\n' > /usr/local/bin/python && \
    chmod 0755 /usr/local/bin/python && \
    for tool in fieldkit pytest ruff mypy; do ln -s /opt/fieldkit/.venv/bin/$tool /usr/local/bin/$tool; done && \
    mkdir -p /sandbox && \
    useradd --uid 1001 --create-home --home-dir /sandbox/agent-home sandbox && \
    mkdir -p /sandbox/workspace /sandbox/output && \
    chown -R sandbox:sandbox /sandbox
ENV HOME=/sandbox/agent-home \
    UV_PROJECT_ENVIRONMENT=/opt/fieldkit/.venv \
    UV_NO_SYNC=1 \
    PYTHONPATH=/sandbox/workspace/src \
    PATH=/opt/fieldkit/.venv/bin:/usr/local/bin:/usr/bin:/bin
USER sandbox
WORKDIR /sandbox/workspace
CMD ["sleep", "infinity"]
