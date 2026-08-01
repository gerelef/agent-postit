"""Structured per-tool-call logging for the HTTP transport.

One JSON line per tool invocation, formatted for `grep` / `jq`:

    {"ts":"2026-08-01T12:34:56.789Z","session_id":null,"tool":"postit.create",
     "dir":"t1","name":"n","outcome":"ok","duration_ms":1.23}

On the error path `error_code` is added:

    {"ts":"...","tool":"postit.create","dir":null,"name":"n","outcome":"error",
     "duration_ms":0.4,"error_code":"already_exists"}

No note bodies are logged — the log records only metadata (tool name, the
`dir` / `name` arguments, outcome, duration). See `docs/http-migration.md`
§5.1.

Design notes:

* `session_id` is logged as `null` in v1. The MCP SDK exposes the per-call
  session id only via an injected `Context` argument on every tool fn,
  which would touch all 13 tool signatures + the test harness for a
  cosmetic field. The structured row itself is the higher-value part;
  revisiting this is tracked as future work.
* The sink is opened once per `ToolLogger` instance and reused for the
  life of the process. Writes are serialised by an internal `threading.Lock`
  because tool bodies run on the default thread pool (see `server._t` /
  `asyncio.to_thread`) and may emit log lines concurrently.
* The default sink is stderr (`POSTIT_LOG=-` or unset). Any other value is
  treated as a filesystem path opened in append mode, line-buffered.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone

ENV = "POSTIT_LOG"
DEFAULT_SINK = "-"


class ToolLogger:
    """Append-only JSON-lines sink for tool-call metadata.

    Construct via `ToolLogger.from_env()` for production (reads `POSTIT_LOG`)
    or directly with an explicit `path` (`"-"` for stderr) for tests.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        if path == "-" or path == "":
            self._file = sys.stderr
            self._owns = False
        else:
            self._file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
            self._owns = True

    @classmethod
    def from_env(cls) -> ToolLogger:
        return cls(os.environ.get(ENV, DEFAULT_SINK))

    def log(
        self,
        *,
        tool: str,
        dir: str | None,
        name: str | None,
        outcome: str,
        duration_ms: float,
        error_code: str | None = None,
        session_id: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "ts": _now_iso(),
            "session_id": session_id,
            "tool": tool,
            "dir": dir,
            "name": name,
            "outcome": outcome,
            "duration_ms": round(duration_ms, 3),
        }
        if error_code is not None:
            row["error_code"] = error_code
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            print(line, file=self._file, flush=True)

    def close(self) -> None:
        if self._owns:
            self._file.close()
            self._owns = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")  # noqa: UP017