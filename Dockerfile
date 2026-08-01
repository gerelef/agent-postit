# syntax=docker/dockerfile:1.7

# ============================================================================
# builder: install app + deps into an isolated venv (no dev tooling in final)
# ============================================================================
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Bootstrap uv in the builder only. (uv itself never ships to the runtime
# image.) uv version is not pinned — only the lockfile is, and uv only runs
# here, so a float is fine.
RUN pip install --no-cache-dir uv

# Manifest-first layer for cache reuse. README.md must be present because
# hatchling reads the project's `readme = "README.md"` field during build.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install locked prod deps into the venv, then install the app itself as a
# wheel (non-editable) so the venv is self-contained and portable to the
# runtime stage without /app present.
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv sync --frozen --no-dev --no-install-project \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-deps .

# ============================================================================
# runtime: minimal image, non-root, no build tooling
# ============================================================================
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    POSTIT_ROOT=/data

# Non-root user with fixed UID/GID 1001 (override at runtime if you need to
# match a host UID for the bind-mounted notes dir, e.g. podman --userns).
RUN groupadd --system --gid 1001 agentpostit \
 && useradd  --system --uid 1001 --gid agentpostit \
             --home-dir /data --shell /usr/sbin/nologin agentpostit \
 && mkdir -p /data \
 && chown -R agentpostit:agentpostit /data

COPY --from=builder --chown=agentpostit:agentpostit /opt/venv /opt/venv

USER agentpostit
WORKDIR /data
VOLUME ["/data"]

LABEL org.opencontainers.image.title="agent-postit" \
      org.opencontainers.image.description="Local stdio MCP server: agent post-it memory on the filesystem." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.usage='Run with: podman run -i --rm -v ~/.agent-postit:/data:Z agent-postit:dev'

# Canonical entrypoint per README: `python -m agent_postit`. The venv on PATH
# makes `python` resolve to the isolated interpreter.
ENTRYPOINT ["python", "-m", "agent_postit"]