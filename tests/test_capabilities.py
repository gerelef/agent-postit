"""`postit.capabilities` tests.

The verb reports the server's full registered tool surface plus its
metadata (name, version, store_root). It does NOT report per-caller
grants — those live in the client's profile config (Zed
`tool_permissions` + per-profile `context_servers.<server>.tools`).
The server has no caller identity (no auth on any transport) and so
cannot honestly echo any caller's grant set; the probe reports the
server's own surface only.

See `docs/complaint-2-capabilities-probe.md` for the design verdict
that bounded the shape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_postit.server import build_server


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def server(root):
    return build_server(root)


def _call(server, **kw):
    return asyncio.run(server.call_tool("postit.capabilities", {"arg": kw or {}}))


def test_capabilities_returns_server_metadata(server, root):
    r = asyncio.run(
        server.call_tool("postit.capabilities", {"arg": {}})
    )
    assert r.is_error is False
    out = r.structured_content["result"]
    assert out["server_name"] == "agent-postit"
    assert out["server_version"]  # non-empty
    # store_root is the absolute resolved path of the fixture root.
    assert out["store_root"] == str(root.resolve())


def test_capabilities_reports_every_registered_tool(server):
    out = _call(server).structured_content["result"]
    listed = {t["name"] for t in out["tools"]}
    # The full surface as of this build. Update if verbs are added/removed.
    expected = {
        "topic.create", "topic.read", "topic.write",
        "postit.create", "postit.append", "postit.overwrite",
        "postit.rename", "postit.delete",
        "postit.read", "postit.read_section", "postit.read_lines",
        "postit.ls", "postit.search", "postit.recent",
        "postit.capabilities",
    }
    assert listed == expected, listed.symmetric_difference(expected)
    assert out["tool_count"] == len(expected)
    assert out["tool_count"] == len(out["tools"])


def test_capabilities_effect_hints_match_decorators(server):
    """The read/destructive/idempotent flags for a known subset match
    what the corresponding `@app.tool(... annotations=_ann(...))`
    decorators declared. Guards against the probe drifting from the
    real annotations over time."""
    out = _call(server).structured_content["result"]
    by_name = {t["name"]: t for t in out["tools"]}

    # read-only cluster
    for n in ("postit.read", "postit.read_section", "postit.read_lines",
              "postit.ls", "postit.search", "postit.recent",
              "topic.read", "postit.capabilities"):
        assert by_name[n]["read_only"] is True, n
        assert by_name[n]["destructive"] is False, n

    # destructive cluster
    for n in ("postit.append", "postit.overwrite", "postit.delete",
              "topic.write"):
        assert by_name[n]["destructive"] is True, n
        assert by_name[n]["read_only"] is False, n


def test_capabilities_is_in_the_handshake_tool_list(server):
    """The probe must itself appear in `tools/list` — it is a real
    tool, advertised like any other, mind you with read-only/idempotent
    annotations of its own."""
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "postit.capabilities" in names


def test_capabilities_idempotent_across_calls(server):
    """Two calls return the same `tool_count` and the same set of tool
    names. The registry is fixed at build time so the probe is a pure
    function of `app._tool_manager._tools`."""
    a = _call(server).structured_content["result"]
    b = _call(server).structured_content["result"]
    assert a["tool_count"] == b["tool_count"]
    assert [t["name"] for t in a["tools"]] == [t["name"] for t in b["tools"]]


def test_capabilities_store_root_is_absolute_resolved(tmp_path: Path):
    """Even if the caller passes a relative-ish path to `build_server`,
    the reported `store_root` is the absolute resolved form."""
    # Build inside a relative-anchored tmp then chdir-style resolve:
    # Path.resolve() on a non-existent path still normalizes; use a real dir.
    root = tmp_path / "data"
    root.mkdir()
    sub = root / "."  # contains a redundant component
    server = build_server(sub)
    out = asyncio.run(
        server.call_tool("postit.capabilities", {"arg": {}})
    ).structured_content["result"]
    assert out["store_root"] == str(root.resolve())
    assert ".." not in out["store_root"]