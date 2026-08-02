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


@pytest.fixture
def prepared(server, root: Path):
    # set up: a topic, two postits in it, one at root
    async def prep():
        await server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "desc"}})
        await server.call_tool("postit.create", {"arg": {"name": "note", "body": "## A\nx", "dir": "t1"}})
        await server.call_tool("postit.create", {"arg": {"name": "rootnote", "body": "y"}})
    asyncio.run(prep())
    return server


EXPECTED_TOOLS = {
    "topic.create",
    "topic.read",
    "topic.write",
    "postit.create",
    "postit.append",
    "postit.overwrite",
    "postit.rename",
    "postit.delete",
    "postit.read",
    "postit.read_section",
    "postit.read_lines",
    "postit.ls",
    "postit.search",
    "postit.recent",
    "postit.capabilities",
}


def test_all_tools_registered(server):
    ts = asyncio.run(server.list_tools())
    names = {t.name for t in ts}
    assert names == EXPECTED_TOOLS, names.symmetric_difference(EXPECTED_TOOLS)


# Per-tool `description` trigger clauses an LLM genuinely needs pre-call.
# Asserting substrings rather than exact strings so cosmetic punctuation
# tweaks don't break the test; the clauses are what matters. See
# `projects/agent-postit/tool-descriptor-simplification`.
EXPECTED_DESCRIPTION_CLAUSES = {
    "postit.read_section": "case-insensitive",
    "postit.search": "recursive",
    "postit.read": "postit.read_section",
}


def test_tool_descriptions_carry_pre_call_clauses(server):
    ts = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in ts}
    for tool_name, clause in EXPECTED_DESCRIPTION_CLAUSES.items():
        assert tool_name in by_name, tool_name
        desc = by_name[tool_name].description or ""
        assert clause in desc, f"{tool_name}: missing clause {clause!r} in {desc!r}"


def test_topic_create_then_read(server):
    r = asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    obj = r.structured_content["result"]
    assert obj["dir"] == "t1"
    assert obj["description"] == "d"
    assert r.is_error is False

    r2 = asyncio.run(server.call_tool("topic.read", {"arg": {"dir": "t1"}}))
    obj2 = r2.structured_content["result"]
    assert obj2["topic"]["description"] == "d"


def test_topic_read_missing_returns_null_topic(server):
    r = asyncio.run(server.call_tool("topic.read", {"arg": {"dir": "nope"}}))
    obj = r.structured_content["result"]
    assert obj["topic"] is None


def test_postit_create_and_read(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    r = asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": "hello", "dir": "t1"}})
    )
    assert r.is_error is False
    obj = r.structured_content["result"]
    assert obj["name"] == "n"
    assert obj["body"] == "hello"

    r2 = asyncio.run(server.call_tool("postit.read", {"arg": {"name": "n", "dir": "t1"}}))
    assert r2.structured_content["result"]["body"] == "hello"


def test_postit_create_reserved_name(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    r = asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "TOPIC", "body": "", "dir": "t1"}})
    )
    obj = r.structured_content["result"]
    assert obj["code"] == "reserved_name"


def test_postit_create_dir_missing(server):
    r = asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": "x", "dir": "nope"}})
    )
    obj = r.structured_content["result"]
    assert obj["code"] == "dir_missing"


def test_postit_append(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": "a", "dir": "t1"}})
    )
    r = asyncio.run(
        server.call_tool("postit.append", {"arg": {"name": "n", "dir": "t1", "content": "b"}})
    )
    assert r.structured_content["result"]["body"] == "a\nb"


def test_postit_overwrite(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": "old", "dir": "t1"}})
    )
    r = asyncio.run(
        server.call_tool("postit.overwrite", {"arg": {"name": "n", "dir": "t1", "content": "new"}})
    )
    assert r.structured_content["result"]["body"] == "new"


def test_postit_rename(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "old", "body": "b", "dir": "t1"}})
    )
    r = asyncio.run(
        server.call_tool("postit.rename", {"arg": {"name": "old", "new_name": "new", "dir": "t1"}})
    )
    assert r.structured_content["result"]["name"] == "new"


def test_postit_delete_and_negative_redelete(server):
    asyncio.run(
        server.call_tool("topic.create", {"arg": {"dir": "t1", "description": "d"}})
    )
    asyncio.run(
        server.call_tool("postit.create", {"arg": {"name": "n", "body": "b", "dir": "t1"}})
    )
    r = asyncio.run(
        server.call_tool("postit.delete", {"arg": {"name": "n", "dir": "t1"}})
    )
    assert r.structured_content["result"]["ok"] is True

    r2 = asyncio.run(
        server.call_tool("postit.delete", {"arg": {"name": "n", "dir": "t1"}})
    )
    assert r2.structured_content["result"]["code"] == "not_found"


def test_postit_read_section(prepared):
    r = asyncio.run(
        prepared.call_tool("postit.read_section", {"arg": {"name": "note", "dir": "t1", "heading": "a"}})
    )
    obj = r.structured_content["result"]
    assert obj["body"] == "## A\nx"


def test_postit_read_lines_ok(prepared):
    r = asyncio.run(
        prepared.call_tool("postit.read_lines", {"arg": {"name": "note", "dir": "t1", "start": 1, "end": 5}})
    )
    obj = r.structured_content["result"]
    assert obj["total_lines"] == 2
    assert obj["end"] == 2
    assert obj["lines"] == "## A\nx"


def test_postit_ls_dir_mode(prepared):
    r = asyncio.run(prepared.call_tool("postit.ls", {"arg": {}}))
    items = r.structured_content["result"]["items"]
    names = {it["name"] for it in items}
    assert "rootnote" in names
    assert "t1" in names
    assert r.structured_content["result"]["note_mode"] is None


def test_postit_ls_note_mode(prepared):
    r = asyncio.run(
        prepared.call_tool("postit.ls", {"arg": {"dir": "t1", "name": "note"}})
    )
    nm = r.structured_content["result"]["note_mode"]
    assert nm is not None
    assert [(h["level"], h["heading"], h["line_no"]) for h in nm["headings"]] == [(2, "A", 1)]


def test_postit_ls_recursive(prepared):
    r = asyncio.run(prepared.call_tool("postit.ls", {"arg": {"recursive": True}}))
    names = {it["name"] for it in r.structured_content["result"]["items"]}
    assert "rootnote" in names
    assert "t1/note" in names


def test_postit_search(prepared):
    r = asyncio.run(
        prepared.call_tool("postit.search", {"arg": {"pattern": "x"}})
    )
    hits = r.structured_content["result"]
    paths = {h["path"] for h in hits}
    assert "t1/note" in paths


def test_postit_recent(prepared):
    r = asyncio.run(prepared.call_tool("postit.recent", {"arg": {"limit": 5}}))
    items = r.structured_content["result"]
    paths = {it["path"] for it in items}
    assert "rootnote" in paths and "t1/note" in paths
    assert len(items) == 2


def test_postit_read_lines_invalid_range(prepared):
    r = asyncio.run(
        prepared.call_tool("postit.read_lines", {"arg": {"name": "note", "dir": "t1", "start": 5, "end": 1}})
    )
    assert r.structured_content["result"]["code"] == "invalid_range"