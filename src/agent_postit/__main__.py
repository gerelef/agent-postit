"""Entrypoint: `python -m agent_postit`.

Resolves the data root via `--root` flag > `POSTIT_ROOT` env >
`~/.agent-postit`, creates it if missing, then hands off to the stdio MCP
server. Exits non-zero on bad CLI input.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .server import run

DEFAULT_ROOT = Path.home() / ".agent-postit"
ENV_VAR = "POSTIT_ROOT"


def _resolve_root(flag_root: str | None) -> Path:
    if flag_root is not None:
        return Path(flag_root).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_ROOT


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-postit",
        description="Local stdio MCP server: agent post-it memory on the filesystem.",
    )
    p.add_argument(
        "--root",
        default=None,
        help=f"Data root dir (overrides ${ENV_VAR} and the default {DEFAULT_ROOT}).",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    p = build_arg_parser()
    args = p.parse_args(argv)
    root = _resolve_root(args.root)
    root.mkdir(parents=True, exist_ok=True)
    run(root)


if __name__ == "__main__":
    main()