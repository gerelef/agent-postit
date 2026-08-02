"""Entrypoint: `python -m agent_postit`.

Resolves the data root via `--root` flag > `POSTIT_ROOT` env >
`~/.agent-postit`, creates it if missing, then hands off to the MCP
server. Default transport is **Streamable HTTP** on `127.0.0.1:8000`
(overridable via `--transport`/`POSTIT_TRANSPORT`, `--host`/`POSTIT_HOST`,
`--port`/`POSTIT_PORT`); `--transport stdio` is retained as a fallback
for tests and one-off local sessions. Exits non-zero on bad CLI input.

No auth is configured on any transport. HTTP binds loopback only —
front it with a TLS-terminating reverse proxy if you need remote reach.
"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

from .server import run

DEFAULT_ROOT = Path.home() / ".agent-postit"
ENV_ROOT = "POSTIT_ROOT"
ENV_TRANSPORT = "POSTIT_TRANSPORT"
ENV_HOST = "POSTIT_HOST"
ENV_PORT = "POSTIT_PORT"

DEFAULT_TRANSPORT = "http"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

VALID_TRANSPORTS = {"stdio", "http", "streamable_http", "streamable-http"}


def _resolve_root(flag_root: str | None) -> Path:
    if flag_root is not None:
        return Path(flag_root).expanduser()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser()
    return DEFAULT_ROOT


def _resolve_transport(flag: str | None) -> str:
    raw = flag if flag is not None else os.environ.get(ENV_TRANSPORT, DEFAULT_TRANSPORT)
    if raw not in VALID_TRANSPORTS:
        raise SystemExit(
            f"unknown transport: {raw!r} (expected one of {sorted(VALID_TRANSPORTS)})"
        )
    return raw


def _resolve_host(flag: str | None) -> str:
    return flag if flag is not None else os.environ.get(ENV_HOST, DEFAULT_HOST)


def _resolve_port(flag: int | None) -> int:
    if flag is not None:
        return flag
    raw = os.environ.get(ENV_PORT)
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise SystemExit(f"{ENV_PORT}={raw!r} is not an integer") from exc
    return DEFAULT_PORT


def _preflight_port_check(host: str, port: int) -> None:
    """Refuse to start a second instance on the same loopback port.

    Attempts a TCP connect to (host, port). If something is already
    listening, exit with a clear error. A refused/timeout connection
    means the port is free — proceed. There is a TOCTOU window between
    this check and uvicorn's own bind; if a process grabs the port in
    between, uvicorn itself prints `EADDRINUSE` and exits non-zero, which
    is an acceptable failure mode. No PID file, no socket handoff.
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        return  # nothing listening — port is free
    raise SystemExit(
        f"agent-postit already running on {host}:{port} — "
        f"refusing to start a second instance"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-postit",
        description="Local MCP server: agent post-it memory on the filesystem.",
    )
    p.add_argument(
        "--root",
        default=None,
        help=f"Data root dir (overrides ${ENV_ROOT} and the default {DEFAULT_ROOT}).",
    )
    p.add_argument(
        "--transport",
        default=None,
        help=(
            f"Transport: 'http' (default), 'stdio'. Overrides ${ENV_TRANSPORT}. "
            "Alias spellings 'streamable_http' / 'streamable-http' are accepted."
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help=f"HTTP bind host (default {DEFAULT_HOST}). Overrides ${ENV_HOST}.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP bind port (default {DEFAULT_PORT}). Overrides ${ENV_PORT}.",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    p = build_arg_parser()
    args = p.parse_args(argv)
    root = _resolve_root(args.root)
    root.mkdir(parents=True, exist_ok=True)
    transport = _resolve_transport(args.transport)
    host = _resolve_host(args.host)
    port = _resolve_port(args.port)
    if transport != "stdio":
        _preflight_port_check(host, port)
    run(root, transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()