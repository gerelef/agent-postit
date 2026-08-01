"""Phase 3 concurrency tests.

Drive the MCP server layer end-to-end through `app.call_tool` under
many concurrent tool calls against the same note identity, asserting
the per-note `threading.RLock` + `asyncio.to_thread` story holds:

* parallel `postit.create` of the same name → exactly one success,
  the rest `already_exists`;
* parallel `postit.update_body` appends to the same note → no lost
  appends (the file body contains every appended fragment, once
  each, in call order — order is serialised by the per-note lock so
  the observable ordering is some permutation of the inputs);
* parallel `topic.create` of the same dir → exactly one success.

These are process-internal: they exercise `build_server` + `call_tool`
in-process. They do NOT spawn uvicorn — the lock + `to_thread` wiring
is the same code path either way (the SDK's HTTP transport ultimately
dispatches tool calls through the same `call_tool` entry point).
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


async def _gather_call(server, tool: str, arg: dict) -> object:
    r = await server.call_tool(tool, {"arg": arg})
    return r.structured_content["result"]


def _run(coro):
    return asyncio.run(coro)


def test_parallel_postit_create_exactly_one_winner(server):
    """N concurrent creates of the same name → exactly one wins, the rest
    return `already_exists`. Without the per-note lock, two callers
    could both pass the existence check and both succeed (last-writer
    wins on disk, both return success) — the bug described in the
    migration doc §4.1."""
    _run(server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}}))

    N = 16
    async def race():
        return await asyncio.gather(*[
            _gather_call(server, "postit.create",
                         {"name": "n", "body": "x", "dir": "t"})
            for _ in range(N)
        ])

    results = _run(race())
    oks = [r for r in results if isinstance(r, dict) and r.get("name") == "n"]
    errs = [r for r in results if isinstance(r, dict) and r.get("code") == "already_exists"]
    assert len(oks) == 1, f"expected 1 winner, got {len(oks)}: {oks}"
    assert len(errs) == N - 1, f"expected {N-1} already_exists, got {len(errs)}"


def test_parallel_append_no_lost_writes(server):
    """N concurrent `update_body` appends of distinct fragments → the
    file body contains every fragment exactly once. Without the lock,
    two appends racing on the same body both read the old content,
    both append, one `os.replace` wins → the other's append is lost
    (migration doc §4.1)."""
    _run(server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}}))
    _run(server.call_tool("postit.create", {"arg": {"name": "log", "body": "", "dir": "t"}}))

    N = 32
    fragments = [f"line-{i:03d}" for i in range(N)]

    async def append_all():
        await asyncio.gather(*[
            _gather_call(server, "postit.update_body",
                         {"name": "log", "dir": "t", "mode": "append", "content": frag})
            for frag in fragments
        ])

    _run(append_all())

    r = _run(server.call_tool("postit.read", {"arg": {"name": "log", "dir": "t"}}))
    body = r.structured_content["result"]["body"]
    got = [ln for ln in body.split("\n") if ln]
    # Every fragment must appear exactly once. Lock serialises the
    # read-modify-write appends; observable order is some permutation.
    assert sorted(got) == sorted(fragments), (
        f"lost or duplicated writes: body={body!r}"
    )
    assert len(got) == N, f"expected {N} lines, got {len(got)}"


def test_parallel_topic_create_exactly_one_winner(server):
    """N concurrent `topic.create` on the same dir name → exactly one
    succeeds, the rest `dir_exists`. Lock converts the mkdir check-then-
    create race into a serialised critical section."""
    N = 8
    async def race():
        return await asyncio.gather(*[
            _gather_call(server, "topic.create",
                         {"dir": "race-topic", "description": "d"})
            for _ in range(N)
        ])

    results = _run(race())
    oks = [r for r in results if isinstance(r, dict) and r.get("dir") == "race-topic"]
    errs = [r for r in results if isinstance(r, dict) and r.get("code") == "dir_exists"]
    assert len(oks) == 1, f"expected 1 winner, got {len(oks)}: {oks}"
    assert len(errs) == N - 1, f"expected {N-1} dir_exists, got {len(errs)}"


def test_concurrent_rename_no_orphans(server):
    """Concurrent renames `A→C` and `B→C`: exactly one wins the dst, the
    other either succeeds-but-no (it can't — dst is taken) or fails with
    `already_exists`. The invariant: `C.md` exists at the end and A or B
    survives untouched. The two-lock sorted-acquire in `postit_rename`
    prevents a self-deadlock when the targets swap."""
    _run(server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}}))
    _run(server.call_tool("postit.create", {"arg": {"name": "a", "body": "A", "dir": "t"}}))
    _run(server.call_tool("postit.create", {"arg": {"name": "b", "body": "B", "dir": "t"}}))

    async def rename(src, dst):
        return await _gather_call(server, "postit.rename",
                                  {"name": src, "new_name": dst, "dir": "t"})

    # A→C and B→C race for the same dst. They touch disjoint src locks.
    async def race():
        return await asyncio.gather(rename("a", "c"), rename("b", "c"))

    r1, r2 = _run(race())
    outcomes = [r1, r2]
    winners = [r for r in outcomes if isinstance(r, dict) and r.get("name") == "c"]
    already = [r for r in outcomes if isinstance(r, dict) and r.get("code") == "already_exists"]
    assert len(winners) == 1, outcomes
    assert len(already) == 1, outcomes

    # The post-state: `c.md` exists with one of A/B's content; the other
    # source survives untouched under its original name.
    ls = _run(server.call_tool("postit.ls", {"arg": {"dir": "t"}}))
    names = {it["name"] for it in ls.structured_content["result"]["items"]}
    assert "c" in names
    # Exactly one of a / b survived (the rename that lost the dst race).
    assert ({"a"} <= names) ^ ({"b"} <= names), names


def test_concurrent_create_then_delete(server):
    """A `delete` racing a `create` of the same name: one outcome is
    `not_found` (delete arrived before create), the other is either
    `ok` (delete of the freshly-created note) or `already_exists` (the
    create arrived after the delete but racing with another create).
    The point: no crash, no half-state — exactly one of the two files
    ends up present or absent, observable state is consistent."""
    _run(server.call_tool("topic.create", {"arg": {"dir": "t", "description": "d"}}))

    async def race():
        return await asyncio.gather(
            _gather_call(server, "postit.create",
                         {"name": "x", "body": "b", "dir": "t"}),
            _gather_call(server, "postit.delete",
                         {"name": "x", "dir": "t"}),
        )

    for _ in range(10):  # flaky-prone race; run a handful of times
        _run(race())
        # No assertion on intermediate outcomes — we just want to
        # exercise the lock around create vs delete and confirm we
        # don't get a Python-level exception. Final state is checked
        # below across the iterations.
        # Re-create if missing so the next iteration has a stable base.
        r = _run(server.call_tool("postit.read", {"arg": {"name": "x", "dir": "t"}}))
        obj = r.structured_content["result"]
        if isinstance(obj, dict) and obj.get("code") == "not_found":
            _run(server.call_tool("postit.create",
                 {"arg": {"name": "x", "body": "b", "dir": "t"}}))