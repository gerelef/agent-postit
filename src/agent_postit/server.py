"""FastMCP / MCPServer app: register every `agent-postit` tool, delegate to
`store` + `search` + `recent`, map errors to `ToolError` so the agent can
branch on `code`.

Implementation note: README L661-664 said to prefer *returning* `ToolError`
from tool fns rather than raising. This works under the current `mcp` SDK:
the returned pydantic model is serialized into `structured_content`. We also
wrap FastMCP in a thin factory `build_server(root)` so tests can spawn an
instance pointed at a tmp `POSTIT_ROOT`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

from mcp.server.mcpserver import MCPServer

from . import models as M
from .paths import PathError
from .store import (
    StoreError,
    NoteInfo,
    SectionResult,
    LinesResult,
    LsDirItem,
    LsNoteModeResult,
    LsPostitItem,
    StoreError as _StoreError,
    TopicInfo,
)
from .sections import Heading as SectionHeading
from .search import search as search_impl, recent as recent_impl
from .search import RecentItem, SearchHit


# --------------------------------------------------------------------------- #
# Helpers: store dataclass -> pydantic model                                  #
# --------------------------------------------------------------------------- #


def _topic(info: TopicInfo) -> M.TopicOut:
    return M.TopicOut(dir=info.dir, description=info.description, mtime=info.mtime, size=info.size)


def _note(info: NoteInfo) -> M.PostitOut:
    return M.PostitOut(name=info.name, dir=info.dir, body=info.body, mtime=info.mtime, size=info.size)


def _heading(h: SectionHeading) -> M.Heading:
    return M.Heading(level=h.level, heading=h.heading, line_no=h.line_no)


def _error(e: Exception) -> M.ToolError:
    code = getattr(e, "code", "invalid_path")
    message = getattr(e, "message", str(e))
    return M.ToolError(code=str(code), message=str(message))


def _ok() -> M.OkOut:
    return M.OkOut()


# --------------------------------------------------------------------------- #
# Server factory                                                               #
# --------------------------------------------------------------------------- #


def build_server(root: Path) -> MCPServer:
    app = MCPServer(name="agent-postit", version="0.1.0")

    # --- Topic verbs --------------------------------------------------------
    @app.tool(name="topic.create", description="Create a topic dir + TOPIC.md description.")
    async def topic_create(arg: M.TopicCreateIn) -> Union[M.TopicOut, M.ToolError]:
        try:
            from . import store as _store
            return _topic(_store.topic_create(root, arg.dir, arg.description))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="topic.read", description="Read a topic's TOPIC.md description.")
    async def topic_read(arg: M.TopicReadIn) -> Union[M.TopicReadOut, M.ToolError]:
        try:
            from . import store as _store
            info = _store.topic_read(root, arg.dir)
            return M.TopicReadOut(
                dir=arg.dir,
                topic=_topic(info) if info is not None else None,
            )
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="topic.write", description="Overwrite a topic's TOPIC.md description.")
    async def topic_write(arg: M.TopicWriteIn) -> Union[M.TopicOut, M.ToolError]:
        try:
            from . import store as _store
            return _topic(_store.topic_write(root, arg.dir, arg.description))
        except (StoreError, PathError) as e:
            return _error(e)

    # --- Postit CRUD --------------------------------------------------------
    @app.tool(name="postit.create", description="Create a new postit note.")
    async def postit_create(arg: M.PostitCreateIn) -> Union[M.PostitOut, M.ToolError]:
        try:
            from . import store as _store
            return _note(_store.postit_create(root, arg.name, arg.body, dir=arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.update_body",
              description="Append or overwrite a postit's body.")
    async def postit_update_body(arg: M.PostitUpdateBodyIn) -> Union[M.PostitOut, M.ToolError]:
        try:
            from . import store as _store
            return _note(
                _store.postit_update_body(
                    root,
                    arg.name,
                    arg.content,
                    dir=arg.dir,
                    mode=arg.mode,
                )
            )
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.rename", description="Rename a postit within the same dir.")
    async def postit_rename(arg: M.PostitRenameIn) -> Union[M.PostitOut, M.ToolError]:
        try:
            from . import store as _store
            return _note(_store.postit_rename(root, arg.name, arg.new_name, dir=arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.delete", description="Delete a postit note (dir survives).")
    async def postit_delete(arg: M.PostitDeleteIn) -> Union[M.OkOut, M.ToolError]:
        try:
            from . import store as _store
            _store.postit_delete(root, arg.name, dir=arg.dir)
            return _ok()
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read", description="Read a postit's full body.")
    async def postit_read(arg: M.PostitReadIn) -> Union[M.PostitOut, M.ToolError]:
        try:
            from . import store as _store
            return _note(_store.postit_read(root, arg.name, dir=arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read_section",
              description="Read a markdown section by heading text (case-insensitive, exact).")
    async def postit_read_section(arg: M.PostitReadSectionIn) -> Union[M.SectionOut, M.ToolError]:
        try:
            from . import store as _store
            r = _store.postit_read_section(root, arg.name, arg.heading, dir=arg.dir, level=arg.level)
            return M.SectionOut(name=r.name, dir=r.dir, heading=r.heading, level=r.level, body=r.body)
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read_lines",
              description="Read a 1-based inclusive line range from a postit body.")
    async def postit_read_lines(arg: M.PostitReadLinesIn) -> Union[M.LinesOut, M.ToolError]:
        try:
            from . import store as _store
            r = _store.postit_read_lines(root, arg.name, arg.start, arg.end, dir=arg.dir)
            return M.LinesOut(
                name=r.name, dir=r.dir, start=r.start, end=r.end,
                total_lines=r.total_lines, lines=r.lines,
            )
        except (StoreError, PathError) as e:
            return _error(e)

    # --- High-level ---------------------------------------------------------
    @app.tool(name="postit.ls",
              description="List dir contents (ls -la style) or list headings of one postit.")
    async def postit_ls(arg: M.PostitLsIn) -> Union[M.LsOut, M.ToolError]:
        try:
            from . import store as _store
            out = _store.postit_ls(root, dir=arg.dir, name=arg.name, recursive=arg.recursive)
            if isinstance(out, LsNoteModeResult):
                return M.LsOut(
                    note_mode=M.LsNoteModeOut(
                        name=out.name,
                        dir=out.dir,
                        total_lines=out.total_lines,
                        headings=[_heading(h) for h in out.headings],
                    )
                )
            return M.LsOut(items=[_ls_to_model(item) for item in out])
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.search",
              description="Regex search across postit names and/or bodies (grep-like).")
    async def postit_search(arg: M.PostitSearchIn) -> Union[list[M.SearchHit], M.ToolError]:
        try:
            hits = search_impl(
                root,
                arg.pattern,
                scope=arg.scope,
                dir=arg.dir,
                recursive=arg.recursive,
                limit=arg.limit,
            )
            return [
                M.SearchHit(
                    path=h.path,
                    name=h.name,
                    body_matches=[M.BodyMatch(line_no=bm.line_no, line=bm.line) for bm in h.body_matches],
                    name_match=h.name_match,
                )
                for h in hits
            ]
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.recent",
              description="Return most-recently-modified postits (always recursive under dir).")
    async def postit_recent(arg: M.PostitRecentIn) -> Union[list[M.RecentItem], M.ToolError]:
        try:
            items = recent_impl(root, limit=arg.limit, dir=arg.dir)
            return [
                M.RecentItem(path=i.path, name=i.name, mtime=i.mtime, size=i.size)
                for i in items
            ]
        except (StoreError, PathError) as e:
            return _error(e)

    return app


def _ls_to_model(item):
    if isinstance(item, LsDirItem):
        return M.LsDirItem(
            name=item.name, has_topic=item.has_topic, topic_preview=item.topic_preview
        )
    if isinstance(item, LsPostitItem):
        return M.LsPostitItem(name=item.name, mtime=item.mtime, size=item.size)
    raise TypeError(f"unknown ls item: {type(item)!r}")


def run(root: Path) -> None:
    """Build the server pointed at `root` and run over stdio."""
    app = build_server(root)
    app.run(transport="stdio")