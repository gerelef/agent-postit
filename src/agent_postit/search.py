"""Regex walker for `postit.search`.

Spec: README.md `## MCP tools` -> `postit.search` row + `## Tool I/O model
shapes` -> `SearchHit` / `BodyMatch`.

Walks the subtree rooted at `dir`. Returns up to `limit` hits, sorted by
path ascending for deterministic output. `TOPIC.md` is never matched.
Default matching is case-insensitive; a user pattern starting with `(?-i)`
disables that (matched by stripping the prefix and compiling without the
IGNORECASE flag). The `(?i)` prefix is also tolerated and stripped so the
user can be explicit without tripping over Python's "global flag" rules.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .paths import (
    NOTE_SUFFIX,
    ROOT,
    TOPIC_FILENAME,
    normalize_dir,
)
from .store import StoreError, _normalize_dir


@dataclass
class BodyMatch:
    line_no: int
    line: str


@dataclass
class SearchHit:
    path: str
    name: str
    body_matches: list[BodyMatch] = field(default_factory=list)
    name_match: bool = False


@dataclass
class SearchInput:
    pattern: str
    scope: str = "both"
    dir: str | None = None
    recursive: bool = True
    limit: int = 50


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile `pattern` with case-insensitivity defaults, honoring `(?-i)`
    / `(?i)` prefixes the spec advertises."""
    flags = re.IGNORECASE
    stripped = pattern
    if stripped.startswith("(?-i)"):
        stripped = stripped[len("(?-i)") :]
        flags = 0
    elif stripped.startswith("(?i)"):
        stripped = stripped[len("(?i)") :]
        flags = re.IGNORECASE
    try:
        return re.compile(stripped, flags)
    except re.error as e:
        raise StoreError("invalid_path", f"bad regex pattern: {e}")


def _is_postit_filename(entry_name: str) -> bool:
    """Postit = lowercase `.md` suffix (case-sensitive) and not a topic marker
    (case-insensitive skip: `Topic.md` / `TOPIC.md` are marker files).
    A hand-created `Foo.MD` on disk is foreign and skipped."""
    if not entry_name.endswith(NOTE_SUFFIX):
        return False
    return entry_name.lower() != TOPIC_FILENAME.lower()


def _rel_path(dir_rel: str, name: str) -> str:
    return name if dir_rel == "" else f"{dir_rel}/{name}"


def search(root: Path, pattern: str, scope: str = "both",
           dir: str | None = None, recursive: bool = True,
           limit: int = 50) -> list[SearchHit]:
    if scope not in ("name", "body", "both"):
        raise StoreError("invalid_path", f"scope must be name|body|both (got {scope!r})")
    if not isinstance(limit, int) or limit < 0:
        raise StoreError("invalid_path", "limit must be a non-negative integer")
    regex = _compile(pattern)

    do_name = scope in ("name", "both")
    do_body = scope in ("body", "both")

    d = _normalize_dir(dir)
    start_dir = root if d == ROOT else root / d
    if not start_dir.is_dir():
        return []

    hits: list[SearchHit] = []

    def visit_dir(abs_dir: Path, rel_dir: str) -> None:
        try:
            entries = sorted(os.scandir(abs_dir), key=lambda e: e.name.encode("utf-8"))
        except OSError:
            return
        for entry in entries:
            full = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    child_rel = entry.name if rel_dir == "" else f"{rel_dir}/{entry.name}"
                    visit_dir(full, child_rel)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if not _is_postit_filename(entry.name):
                continue
            name = entry.name[: -len(NOTE_SUFFIX)]
            n_match = do_name and (regex.search(name) is not None)
            body_matches: list[BodyMatch] = []
            if do_body:
                try:
                    with open(full, "r", encoding="utf-8", newline="") as f:
                        body = f.read()
                except OSError:
                    body = ""
                if body:
                    for ln_no, line in enumerate(body.split("\n"), start=1):
                        if regex.search(line):
                            # Strip trailing newline as normal `split` already
                            # dropped \n; line is the text without terminator.
                            body_matches.append(BodyMatch(line_no=ln_no, line=line))
            if n_match or body_matches:
                hits.append(
                    SearchHit(
                        path=_rel_path(rel_dir, name),
                        name=name,
                        body_matches=body_matches,
                        name_match=n_match,
                    )
                )

    visit_dir(start_dir, d)

    hits.sort(key=lambda h: h.path.encode("utf-8"))
    if limit == 0:
        return []
    return hits[:limit]


# --------------------------------------------------------------------------- #
# `postit.recent`                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class RecentItem:
    path: str
    name: str
    mtime: float
    size: int


def recent(root: Path, limit: int = 10, dir: str | None = None) -> list[RecentItem]:
    """Walk subtree rooted at `dir` (always recursive), sort by mtime desc
    with `path` ascending as tiebreaker, return top `limit` postits.

    Spec: README.md `## MCP tools` -> `postit.recent` row + `## mtime as
    ordering key`. `TOPIC.md` is skipped. No bodies returned — caller reads
    selectively via `postit.read` / `postit.read_section`.
    """
    if not isinstance(limit, int) or limit < 0:
        raise StoreError("invalid_path", "limit must be a non-negative integer")
    d = _normalize_dir(dir)
    start_dir = root if d == ROOT else root / d
    if not start_dir.is_dir():
        return []

    items: list[RecentItem] = []

    def visit_dir(abs_dir: Path, rel_dir: str) -> None:
        try:
            entries = list(os.scandir(abs_dir))
        except OSError:
            return
        for entry in entries:
            full = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                child_rel = entry.name if rel_dir == "" else f"{rel_dir}/{entry.name}"
                visit_dir(full, child_rel)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if not _is_postit_filename(entry.name):
                continue
            name = entry.name[: -len(NOTE_SUFFIX)]
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            items.append(
                RecentItem(
                    path=_rel_path(rel_dir, name),
                    name=name,
                    mtime=st.st_mtime,
                    size=st.st_size,
                )
            )

    visit_dir(start_dir, d)

    # mtime desc, tiebreak path asc (byte order).
    items.sort(key=lambda it: (-it.mtime, it.path.encode("utf-8")))
    if limit == 0:
        return []
    return items[:limit]