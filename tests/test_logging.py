"""Phase 4 structured-log tests.

Drives `build_server` in-process (no uvicorn, no transport) with the
`ToolLogger` pointed at a tmp file, fires one successful and one failed
tool call, and asserts the JSON-lines emitted match the schema:

* required keys always present: `ts`, `session_id`, `tool`, `dir`,
  `name`, `outcome`, `duration_ms`;
* `error_code` present only on the error row;
* `outcome` is `"ok"` on success, `"error"` on a returned `ToolError`;
* no note body content leaks into the log.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_postit.log import ToolLogger
from agent_postit.server import build_server


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


def _read_log_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_ok_call_emits_log_row(root: Path, tmp_path: Path):
    log_path = tmp_path / "postit.log"
    logger = ToolLogger(str(log_path))
    server = build_server(root, logger=logger)

    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}})
    )
    logger.close()

    rows = _read_log_lines(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {"ts", "session_id", "tool", "dir", "name", "outcome", "duration_ms"}
    assert row["tool"] == "topic.create"
    assert row["dir"] == "t"
    assert row["name"] is None  # topic.create has no `name` arg
    assert row["outcome"] == "ok"
    assert row["session_id"] is None  # v1: session id unavailable without injected Context
    assert isinstance(row["duration_ms"], (int, float))
    assert row["duration_ms"] >= 0
    assert "error_code" not in row


def test_error_call_emits_error_code(root: Path, tmp_path: Path):
    log_path = tmp_path / "postit.log"
    logger = ToolLogger(str(log_path))
    server = build_server(root, logger=logger)

    # Create a topic, then attempt a conflicting `topic.create` with a
    # different description -> `dir_exists` ToolError. (Same description
    # would be an idempotent no-op success as of Stage-2.)
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "dup", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "dup", "description": "DIFFERENT"}})
    )
    logger.close()

    rows = _read_log_lines(log_path)
    assert len(rows) == 2
    ok_row, err_row = rows
    assert ok_row["outcome"] == "ok"
    assert err_row["outcome"] == "error"
    assert err_row["error_code"] == "dir_exists"


def test_no_body_in_log(root: Path, tmp_path: Path):
    """The structured log must never carry note body content.

    Only metadata: tool name, dir, name, outcome, duration_ms, error_code.
    Bodies are private. The log file must not contain the body string
    after a create+read cycle.
    """
    log_path = tmp_path / "postit.log"
    logger = ToolLogger(str(log_path))
    server = build_server(root, logger=logger)

    body = "SECRET-BODY-CONTENT-NOT-FOR-THE-LOG"
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": body, "dir": "t"}})
    )
    asyncio.run(server.call_tool("postit.read", {"arg": {"name": "n", "dir": "t"}}))
    logger.close()

    raw = log_path.read_text(encoding="utf-8")
    assert "SECRET-BODY-CONTENT-NOT-FOR-THE-LOG" not in raw
    # `name` IS metadata and should appear — the assertion above is body-only.
    rows = _read_log_lines(log_path)
    assert len(rows) == 3
    create_row = next(r for r in rows if r["tool"] == "postit.create")
    assert create_row["name"] == "n"
    assert create_row["dir"] == "t"