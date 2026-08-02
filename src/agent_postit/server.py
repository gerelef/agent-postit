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

import asyncio
import functools
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import models as M
from .log import ToolLogger
from .paths import PathError
from .search import recent as recent_impl
from .search import search as search_impl
from .sections import Heading as SectionHeading
from .store import (
    LsDirItem,
    LsNoteModeResult,
    LsPostitItem,
    NoteInfo,
    StoreError,
    TopicInfo,
)

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
# Tool annotations                                                            #
# --------------------------------------------------------------------------- #
#
# `ToolAnnotations` is behavior metadata (MCP spec 2026-07-28). Clients MUST
# treat them as untrusted, but they drive host UX (confirmation dialogs,
# auto-grant batches, retry-safety on transient errors). `open_world_hint`
# is False for every tool — agent-postit is a closed system scoped to
# `POSTIT_ROOT`. See `references/mcp/metadata-enhancement-roadmap` Stage 2.


def _ann(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


# --------------------------------------------------------------------------- #
# Server `instructions` (MCP spec 2026-07-28 `initialize` result)            #
# --------------------------------------------------------------------------- #
#
# Short paragraph injected into the model's context at session start, before
# any `tools/list` lands. Cheapest leverage we have: the model picks up the
# 'address notes by (dir, name)' + 'topic-first' contract before reading a
# single tool def. Keep it <~60 words; long instructions dilute.

INSTRUCTIONS = (
    "agent-postit: store and recall short markdown notes filed under topics "
    "on the local filesystem. Address notes by (dir, name) - there are no "
    "integer IDs. Topics must exist before notes can be written into them. "
    "Call postit.recent at session start to reload context."
)


# --------------------------------------------------------------------------- #
# Async-wrapper helpers                                                        #
# --------------------------------------------------------------------------- #
#
# The store layer is synchronous (os.scandir / open / Path.stat) and holds
# per-note `threading.RLock`s for write-path tools. Calling these sync fns
# directly inside an `async def` tool fn would block the event-loop thread
# for the duration of every tool call — fine under stdio (one call at a
# time), wrong under HTTP (many concurrent calls). We push every store call
# through `asyncio.to_thread` so it runs on the default thread pool, where
# `threading.RLock` is the right primitive and the event loop stays free to
# serve other sessions.


def _t(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Tool-call instrumentation (structured log)                                  #
# --------------------------------------------------------------------------- #
#
# `Tool.fn` is a plain pydantic field on the SDK's `Tool` model and is read
# again on every `call_tool`, so post-registration we can swap it for a
# wrapper that records the call and still delegates to the original. This
# keeps the verbose per-tool bodies unchanged and avoids re-declaring the
# input/output schemas — `Tool.from_function` already pinned those at
# registration time and the wrapper carries the same signature via
# `functools.wraps`. The schema is not re-derived from `fn` afterward.
#
# Outcome detection: our tool fns *return* `M.ToolError` on the error path
# rather than raising (README L661-664). The wrapper treats either a
# `ToolError` return or an escaped exception as `outcome == "error"`.


def _wrap_logged(tool_name: str, fn, logger: ToolLogger):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        # The SDK calls tool fns as `fn(arg=<pydantic model>)` (see
        # `FuncMetadata.call_fn_with_arg_validation`): each declared
        # parameter becomes a kwarg whose value is the validated input
        # model. Our tools take a single `arg` param, so the model lives
        # at `kwargs["arg"]`; fall back to positional in case the SDK ever
        # dispatches that way.
        arg_obj = kwargs.get("arg") if kwargs else (args[0] if args else None)
        d = getattr(arg_obj, "dir", None)
        n = getattr(arg_obj, "name", None)
        t0 = time.perf_counter()
        outcome = "ok"
        error_code: str | None = None
        try:
            result = await fn(*args, **kwargs)
            if isinstance(result, M.ToolError):
                outcome = "error"
                error_code = result.code
            return result
        except Exception:
            outcome = "error"
            error_code = "exception"
            raise
        finally:
            logger.log(
                tool=tool_name,
                dir=d,
                name=n,
                outcome=outcome,
                duration_ms=(time.perf_counter() - t0) * 1000,
                error_code=error_code if outcome == "error" else None,
            )

    return wrapper


def _instrument(app: MCPServer, logger: ToolLogger) -> None:
    # `app._tool_manager._tools` is the SDK's name->Tool registry. Patching
    # each Tool's `fn` is safe because `Tool.run` reads `self.fn` fresh on
    # every call and the input/output schemas were already locked in by
    # `from_function` at registration.
    for name, tool in app._tool_manager._tools.items():
        tool.fn = _wrap_logged(name, tool.fn, logger)


# --------------------------------------------------------------------------- #
# Server factory                                                               #
# --------------------------------------------------------------------------- #


def build_server(root: Path, *, logger: ToolLogger | None = None) -> MCPServer:
    if logger is None:
        logger = ToolLogger.from_env()
    app = MCPServer(name="agent-postit", version="1.0.0", instructions=INSTRUCTIONS)

    # --- Topic verbs --------------------------------------------------------
    @app.tool(name="topic.create",
              title="Topic: Create",
              description="Create a new topic directory with a short description. Use when starting a new concern, so later notes have a place to go.",
              annotations=_ann(idempotent=True))
    async def topic_create(arg: M.TopicCreateIn) -> M.TopicOut | M.ToolError:
        try:
            from . import store as _store
            return _topic(await _t(_store.topic_create, root, arg.dir, arg.description))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="topic.read",
              title="Topic: Read",
              description="Read a topic's description.",
              annotations=_ann(read_only=True, idempotent=True))
    async def topic_read(arg: M.TopicReadIn) -> M.TopicReadOut | M.ToolError:
        try:
            from . import store as _store
            info = await _t(_store.topic_read, root, arg.dir)
            return M.TopicReadOut(
                dir=arg.dir,
                topic=_topic(info) if info is not None else None,
            )
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="topic.write",
              title="Topic: Write",
              description="Overwrite a topic's description.",
              annotations=_ann(destructive=True, idempotent=True))
    async def topic_write(arg: M.TopicWriteIn) -> M.TopicOut | M.ToolError:
        try:
            from . import store as _store
            return _topic(await _t(_store.topic_write, root, arg.dir, arg.description))
        except (StoreError, PathError) as e:
            return _error(e)

    # --- Postit CRUD --------------------------------------------------------
    @app.tool(name="postit.create",
              title="Postit: Create",
              description="Create a new postit note. Use after learning something you'll want to recall across sessions (a cliffnote, a decision, a reference URL).",
              annotations=_ann())
    async def postit_create(arg: M.PostitCreateIn) -> M.PostitOut | M.ToolError:
        try:
            from . import store as _store
            return _note(await _t(_store.postit_create, root, arg.name, arg.body, arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.append",
              title="Postit: Append",
              description="Append content to the end of an existing postit's body without rewriting it. Read-modify-write under a per-note lock: safe under concurrent appends. A newline is inserted between the existing body and the new content when the existing body is non-empty and lacks a trailing newline.",
              annotations=_ann(destructive=True))
    async def postit_append(arg: M.PostitAppendIn) -> M.PostitOut | M.ToolError:
        try:
            from . import store as _store
            return _note(await _t(_store.postit_append, root, arg.name, arg.content, arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.overwrite",
              title="Postit: Overwrite",
              description="Replace a postit's entire body with new content. The previous content is discarded atomically (tmp + fsync + rename). Use postit.append when you want to add to the existing body instead of replacing it.",
              annotations=_ann(destructive=True))
    async def postit_overwrite(arg: M.PostitOverwriteIn) -> M.PostitOut | M.ToolError:
        try:
            from . import store as _store
            return _note(await _t(_store.postit_overwrite, root, arg.name, arg.content, arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.rename",
              title="Postit: Rename",
              description="Rename a postit within the same topic.",
              annotations=_ann(idempotent=True))
    async def postit_rename(arg: M.PostitRenameIn) -> M.PostitOut | M.ToolError:
        try:
            from . import store as _store
            return _note(await _t(_store.postit_rename, root, arg.name, arg.new_name, arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.delete",
              title="Postit: Delete",
              description="Delete a postit note.",
              annotations=_ann(destructive=True, idempotent=True))
    async def postit_delete(arg: M.PostitDeleteIn) -> M.OkOut | M.ToolError:
        try:
            from . import store as _store
            await _t(_store.postit_delete, root, arg.name, arg.dir)
            return _ok()
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read",
              title="Postit: Read",
              description="Read a postit's full body. For large bodies prefer `postit.read_section` or `postit.read_lines` instead.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_read(arg: M.PostitReadIn) -> M.PostitOut | M.ToolError:
        try:
            from . import store as _store
            return _note(await _t(_store.postit_read, root, arg.name, arg.dir))
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read_section",
              title="Postit: Read Section",
              description="Read one section of a postit by heading text. Match is case-insensitive and exact (not a substring); use `level` to disambiguate same-text headings at different depths.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_read_section(arg: M.PostitReadSectionIn) -> M.SectionOut | M.ToolError:
        try:
            from . import store as _store
            r = await _t(
                _store.postit_read_section,
                root,
                arg.name,
                arg.heading,
                arg.dir,
                arg.level,
            )
            return M.SectionOut(name=r.name, dir=r.dir, heading=r.heading, level=r.level, body=r.body)
        except (StoreError, PathError) as e:
            return _error(e)

    @app.tool(name="postit.read_lines",
              title="Postit: Read Lines",
              description="Read a line range from a postit.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_read_lines(arg: M.PostitReadLinesIn) -> M.LinesOut | M.ToolError:
        try:
            from . import store as _store
            r = await _t(_store.postit_read_lines, root, arg.name, arg.start, arg.end, arg.dir)
            return M.LinesOut(
                name=r.name, dir=r.dir, start=r.start, end=r.end,
                total_lines=r.total_lines, lines=r.lines,
            )
        except (StoreError, PathError) as e:
            return _error(e)

    # --- High-level ---------------------------------------------------------
    @app.tool(name="postit.ls",
              title="Postit: List",
              description="List topic contents, or list headings of one postit.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_ls(arg: M.PostitLsIn) -> M.LsOut | M.ToolError:
        try:
            from . import store as _store
            out = await _t(
                _store.postit_ls,
                root,
                arg.dir,
                arg.name,
                arg.recursive,
            )
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
              title="Postit: Search",
              description="Search postit names and bodies by regex. Scope with `dir` (subtree root) and `recursive`; `limit` caps hit count. Pattern is case-insensitive unless prefixed `(?-i)`.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_search(arg: M.PostitSearchIn) -> list[M.SearchHit] | M.ToolError:
        try:
            hits = await _t(
                search_impl,
                root,
                arg.pattern,
                arg.scope,
                arg.dir,
                arg.recursive,
                arg.limit,
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
              title="Postit: Recent",
              description="Return most-recently-modified postits. Call at session start to reload context before deciding what to look up.",
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_recent(arg: M.PostitRecentIn) -> list[M.RecentItem] | M.ToolError:
        try:
            items = await _t(recent_impl, root, arg.limit, arg.dir)
            return [
                M.RecentItem(path=i.path, name=i.name, mtime=i.mtime, size=i.size)
                for i in items
            ]
        except (StoreError, PathError) as e:
            return _error(e)

    # --- Capabilities probe ------------------------------------------------
    @app.tool(name="postit.capabilities",
              title="Postit: Capabilities",
              description=(
                  "Return this server's full registered tool surface plus server "
                  "metadata (name, version, store_root). Read-only summary of what "
                  "the server can execute. Does NOT report per-caller grants - which "
                  "tools THIS caller is allowed to invoke is governed client-side by "
                  "the editor's profile config (Zed: `tool_permissions` + per-profile "
                  "`context_servers.<server>.tools`), not by anything this server can "
                  "report. Use `tools/list` for full input schemas; this verb is the "
                  "lighter effect-hint summary. Safe to call at session start "
                  "alongside `postit.recent` to pin the surface you're talking to."
              ),
              annotations=_ann(read_only=True, idempotent=True))
    async def postit_capabilities(arg: M.CapabilitiesIn) -> M.CapabilitiesOut | M.ToolError:
        items = []
        for name, tool in app._tool_manager._tools.items():
            a = tool.annotations
            items.append(M.ToolSummary(
                name=name,
                title=tool.title,
                read_only=bool(a.read_only_hint) if a else False,
                destructive=bool(a.destructive_hint) if a else False,
                idempotent=bool(a.idempotent_hint) if a else False,
                open_world=bool(a.open_world_hint) if a else False,
            ))
        return M.CapabilitiesOut(
            server_name=app.name,
            server_version=app.version,
            store_root=str(root.resolve()),
            tool_count=len(items),
            tools=items,
        )

    _instrument(app, logger)
    return app


def _ls_to_model(item):
    if isinstance(item, LsDirItem):
        return M.LsDirItem(
            name=item.name, has_topic=item.has_topic, topic_preview=item.topic_preview
        )
    if isinstance(item, LsPostitItem):
        return M.LsPostitItem(name=item.name, mtime=item.mtime, size=item.size)
    raise TypeError(f"unknown ls item: {type(item)!r}")


# --------------------------------------------------------------------------- #
# HTTP app assembly: Streamable HTTP + /healthz                               #
# --------------------------------------------------------------------------- #
#
# `streamable_http_app(...)` returns a Starlette app whose only route is the
# MCP mount at `streamable_http_path` (we pass `/mcp`) and whose lifespan
# starts the session-manager task group. We must serve *that* app directly
# so the lifespan runs — wrapping it in an outer Starlette would skip the
# inner's lifespan and crash with `RuntimeError: Task group is not
# initialized. Make sure to use run().` at the first request. So we splice
# a `/healthz` `Route` onto the inner app's router ahead of the MCP mount
# and return the inner app unchanged. Route matching is declaration order,
# so `/healthz` wins over the catch-all MCP route.
#
# `GET /healthz` returns `200` with body `"ok"`. No auth (see §3 of the
# migration doc); loopback bind is the caller's responsibility.


async def _healthz(request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok", status_code=200)


def build_http_app(root: Path, *, host: str = "127.0.0.1"):
    """Build the full HTTP ASGI app: MCP at `/mcp` + liveness at `/healthz`.

    Returns the Starlette instance produced by `streamable_http_app(...)`
    with an extra `Route("/healthz", ...)` spliced onto its router ahead of
    the MCP mount. Serve it directly with uvicorn so the inner lifespan
    (session-manager task group) runs. Tests can drive it directly without
    spawning a server.
    """
    from starlette.routing import Route

    app = build_server(root)
    starlette_app = app.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        host=host,
    )
    # Prepend so /healthz matches first. `Starlette.router.routes` is the
    # list the router iterates at request time.
    starlette_app.router.routes.insert(0, Route("/healthz", _healthz, methods=["GET"]))
    return starlette_app


def run(root: Path, *, transport: str = "http", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Build the server pointed at `root` and run it over the chosen transport.

    `transport="stdio"` is the retained fallback (one tool call at a time,
    in-process). `transport="http"` (and its alias spellings) hosts the
    Streamable HTTP server on `host:port` via uvicorn, using the SDK's
    `streamable_http_app(json_response=True, ...)` Starlette mount with a
    sibling `/healthz` route added (see `build_http_app`). No auth; loopback
    bind is the responsibility of the caller (`__main__` defaults `host`
    to `127.0.0.1`).
    """
    if transport == "stdio":
        build_server(root).run(transport="stdio")
        return
    if transport in ("http", "streamable_http", "streamable-http"):
        starlette_app = build_http_app(root, host=host)
        import uvicorn

        uvicorn.run(starlette_app, host=host, port=port, log_level="info")
        return
    raise SystemExit(f"unknown transport: {transport!r}")