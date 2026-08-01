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
# runtime: minimal image, no build tooling, runs as rootless container root
# ============================================================================
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    POSTIT_ROOT=/data \
    POSTIT_TRANSPORT=http \
    POSTIT_HOST=0.0.0.0 \
    POSTIT_PORT=8000

# NOTE: the container bind is 0.0.0.0 *inside the container netns*, NOT on
# the host. Podman/docker port-forwarding (`-p 127.0.0.1:8000:8000`) governs
# host exposure; the container-internal bind must be 0.0.0.0 so podman's
# proxy can reach the listening socket. Binding 127.0.0.1 inside the
# container rejects forwarded traffic with `connection reset` — only
# in-container processes (e.g. a sidecar) could reach it. The v1 trust
# boundary is preserved by binding loopback on the host publish side.

# No `USER` clause: in rootless podman (the supported deployment) the
# container process runs as uid 0 *inside* the container netns, which maps
# to the *invoking host user's uid* by default — so the bind-mounted
# `~/.agent-postit` is owned by you on the host and Just Works without any
# chown/subuid dance. (Pinning a fixed in-container uid like 1001 instead
# maps to a host subuid, NOT your uid, and breaks bind-mount writes.) For
# system podman / docker where container root is real root, override with
# `--user $(id -u):$(id -g)` so files land as your host uid.
RUN mkdir -p /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /data
VOLUME ["/data"]

LABEL org.opencontainers.image.title="agent-postit" \
      org.opencontainers.image.description="Local HTTP MCP server (loopback): agent post-it memory on the filesystem." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.usage='Run with: podman run --rm -p 127.0.0.1:8000:8000 -v ~/.agent-postit:/data:Z agent-postit:dev'

# Liveness probe. Uses the base image's `urllib` so we keep the slim
# runtime dependency-free (no curl). Probes 127.0.0.1 *inside* the container —
# fine because the HEALTHCHECK runs in the container's own netns where
# uvicorn is reachable on loopback. Unauthenticated endpoint (see README).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request as u, os; u.urlopen('http://127.0.0.1:'+os.environ.get('POSTIT_PORT','8000')+'/healthz', timeout=2)" || exit 1

# Container listens on 8000 by default. Bind loopback on the host side:
#   podman run -p 127.0.0.1:8000:8000 ...
# Use `-p 0.0.0.0:8000:8000` only if you intend to front it with a reverse
# proxy on the same host (still no auth — see the Auth section in README).
EXPOSE 8000

# Canonical entrypoint per README: `python -m agent_postit`. The venv on PATH
# makes `python` resolve to the isolated interpreter.
ENTRYPOINT ["python", "-m", "agent_postit"]