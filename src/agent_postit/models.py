"""Pydantic schemas for every MCP tool's input and return.

These match README.md `## Tool I/O model shapes` verbatim. FastMCP uses the
input models as tool argument types and serializes the return models into
the MCP tool-result payload. The store layer returns its own plain
dataclasses; `server.py` is responsible for the store->model conversion.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel


# --------------------------------------------------------------------------- #
# Topic verbs                                                                  #
# --------------------------------------------------------------------------- #


class TopicCreateIn(BaseModel):
    dir: str
    description: str  # may be ""


class TopicReadIn(BaseModel):
    dir: str


class TopicWriteIn(BaseModel):
    dir: str
    description: str  # overwrite-only


class TopicOut(BaseModel):
    dir: str
    description: str
    mtime: float
    size: int


class TopicReadOut(BaseModel):
    """Wrapper for `topic.read` so that `null` (no topic) is a structured
    object, not bare `None` (some MCP clients choke on bare null)."""

    dir: str
    topic: TopicOut | None = None


# --------------------------------------------------------------------------- #
# Postit CRUD                                                                  #
# --------------------------------------------------------------------------- #


class PostitCreateIn(BaseModel):
    name: str
    body: str  # may be ""
    dir: str | None = None  # default root


class PostitUpdateBodyIn(BaseModel):
    name: str
    dir: str | None = None
    mode: Literal["append", "overwrite"] = "overwrite"
    content: str


class PostitRenameIn(BaseModel):
    name: str
    dir: str | None = None
    new_name: str


class PostitDeleteIn(BaseModel):
    name: str
    dir: str | None = None


class PostitReadIn(BaseModel):
    name: str
    dir: str | None = None


class PostitReadSectionIn(BaseModel):
    name: str
    dir: str | None = None
    heading: str
    level: int = 2  # 1..6


class PostitReadLinesIn(BaseModel):
    name: str
    dir: str | None = None
    start: int  # 1-based
    end: int   # inclusive


class OkOut(BaseModel):
    ok: bool = True


class PostitOut(BaseModel):
    name: str
    dir: str
    body: str
    mtime: float
    size: int


class SectionOut(BaseModel):
    name: str
    dir: str
    heading: str
    level: int
    body: str | None  # None if no match


class LinesOut(BaseModel):
    name: str
    dir: str
    start: int
    end: int
    total_lines: int
    lines: str


# --------------------------------------------------------------------------- #
# High-level                                                                   #
# --------------------------------------------------------------------------- #


class PostitLsIn(BaseModel):
    dir: str | None = None
    name: str | None = None
    recursive: bool = False


class LsDirItem(BaseModel):
    type: Literal["dir"] = "dir"
    name: str
    has_topic: bool
    topic_preview: str | None = None


class LsPostitItem(BaseModel):
    type: Literal["postit"] = "postit"
    name: str
    mtime: float
    size: int


class Heading(BaseModel):
    level: int      # 1..6
    heading: str    # parsed text (close-form # stripped, trimmed)
    line_no: int    # 1-based


class LsNoteModeOut(BaseModel):
    name: str
    dir: str
    total_lines: int
    headings: list[Heading]


class LsOut(BaseModel):
    """Wrapper for `postit.ls` dir-mode results so FastMCP serializes the
    heterogeneous list into `structured_content` rather than falling back to
    multi-text content blobs."""

    items: list[Union[LsDirItem, LsPostitItem]] | None = None
    note_mode: LsNoteModeOut | None = None


class PostitSearchIn(BaseModel):
    pattern: str
    scope: Literal["name", "body", "both"] = "both"
    dir: str | None = None
    recursive: bool = True
    limit: int = 50


class BodyMatch(BaseModel):
    line_no: int
    line: str


class SearchHit(BaseModel):
    path: str
    name: str
    body_matches: list[BodyMatch]
    name_match: bool


class PostitRecentIn(BaseModel):
    limit: int = 10
    dir: str | None = None


class RecentItem(BaseModel):
    path: str
    name: str
    mtime: float
    size: int


# --------------------------------------------------------------------------- #
# Error shape                                                                  #
# --------------------------------------------------------------------------- #


class ToolError(BaseModel):
    code: str   # snake_case, from the spec error table
    message: str


# Convenience: a tool result is either the success payload or a ToolError. We
# do not encode the union at the type layer (FastMCP serializes whatever we
# return); this alias is for readability inside `server.py`.
ToolResult = Union[BaseModel, ToolError]