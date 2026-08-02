"""Phase 1 HTTP smoke test.

Spawns the Starlette app (built from `build_server` +
`streamable_http_app(json_response=True, ...)`) on a free loopback port
via uvicorn, then exercises the request-validation behaviour of the
Streamable HTTP middleware:

* a well-formed `initialize` JSON-RPC POST returns 200 and a
  `application/json` body (the negotiated response mode for
  `json_response=True`);
* a POST missing `Content-Type: application/json` is rejected by the
  SDK middleware with 400 — `TransportSecurityMiddleware` intercepts
  every POST and rejects anything that is missing or not
  `application/json` before the json-or-sse accept negotiation in
  `_handle_post_request` (whose 415 branch is `# pragma: no cover`);
* a POST missing the required `Accept` header is rejected with 406 Not
  Acceptable.
* `GET /healthz` returns 200 `"ok"` (mounted on the outer Starlette,
  outside `/mcp`).

No auth, no `--token`, no 401 paths — they don't exist (see
`docs/http-migration.md` §3). This test never touches `server.run` nor
`__main__.main`: it drives the Starlette app directly so it is stable
against changes in CLI plumbing.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from agent_postit.server import build_http_app

INIT_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    }
).encode()

JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"uvicorn did not start on {host}:{port}")


@pytest.fixture
def http_server(root):
    port = _free_port()
    app = build_http_app(root, host="127.0.0.1")
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port("127.0.0.1", port)
    base = f"http://127.0.0.1:{port}/mcp"
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode()


def test_initialize_returns_200(http_server):
    status, ctype, _body = _post(http_server, INIT_BODY, JSON_HEADERS)
    assert status == 200, (status, _body)
    assert ctype.startswith("application/json"), ctype


def test_initialize_advertises_instructions(http_server):
    """Stage 3: server `instructions` must reach the client in the
    `initialize` result so the model ingests the topic-first /(dir, name)/
    `postit.recent` contract before any `tools/list`. Asserts the field is
    present, non-empty, and contains the three expected keywords."""
    status, _ctype, body = _post(http_server, INIT_BODY, JSON_HEADERS)
    assert status == 200, (status, body)
    payload = json.loads(body)
    result = payload.get("result", {})
    instr = result.get("instructions")
    assert isinstance(instr, str) and instr, f"instructions missing/empty: {payload!r}"
    # Keyword smoke — values match INSTRUCTIONS constant in server.py.
    for keyword in ("topic", "dir, name", "postit.recent"):
        assert keyword in instr, f"instructions missing {keyword!r}: {instr!r}"


def test_missing_content_type_rejected(http_server):
    # The SDK's `TransportSecurityMiddleware` validates Content-Type for
    # every POST independently of `enable_dns_rebinding_protection`, and
    # rejects anything that is missing or not `application/json` with 400
    # *before* the json-or-sse accept negotiation in `_handle_post_request`
    # (whose 415 branch is `# pragma: no cover` for this reason).
    headers = {k: v for k, v in JSON_HEADERS.items() if k != "Content-Type"}
    status, _ctype, _body = _post(http_server, INIT_BODY, headers)
    assert status == 400, (status, _body)


def test_missing_accept_rejected(http_server):
    headers = {k: v for k, v in JSON_HEADERS.items() if k != "Accept"}
    status, _ctype, _body = _post(http_server, INIT_BODY, headers)
    assert status == 406, (status, _body)


def test_healthz_returns_ok(http_server):
    # /healthz is mounted on the outer Starlette ahead of the MCP mount,
    # so it answers 200 "ok" outside the JSON-RPC path. No auth.
    base_root = http_server.rsplit("/mcp", 1)[0]
    with urllib.request.urlopen(f"{base_root}/healthz", timeout=2) as resp:
        assert resp.status == 200
        assert resp.read() == b"ok"


def test_transport_dispatch_unknown():
    from agent_postit.server import run

    with pytest.raises(SystemExit):
        run(Path("/nonexistent-does-not-matter"), transport="bogus")