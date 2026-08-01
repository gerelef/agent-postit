"""Filesystem operations for agent-postit.

All user-facing store functions take the root dir (`pathlib.Path`) and the
*unnormalized* user args (so the store layer is the single place where
normalization + validation happens for fs ops). They return plain dataclasses;
the server layer maps these to FastMCP tool return shapes. Errors raise
`StoreError` (or `path.InvalidNameError` / `path.InvalidPathError`), all of
which carry `.code` + `.message` for serialization to a `ToolError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import (
    InvalidNameError,
    InvalidPathError,
    ROOT,
    TOPIC_BASENAME,
    TOPIC_FILENAME,
    NOTE_SUFFIX,
    normalize_dir,
    validate_name,
)
from .sections import Heading, parse_headings, read_section


def _validate_name(name: str) -> str:
    """Run `paths.validate_name` and re-raise its error as StoreError."""
    try:
        return validate_name(name)
    except InvalidNameError as e:
        raise StoreError(e.code, e.message) from None


def _normalize_dir(dir: str | None) -> str:
    """Normalize `dir`; raise InvalidPathError as StoreError."""
    try:
        return normalize_dir(dir if dir is not None else "")
    except InvalidPathError as e:
        raise StoreError(e.code, e.message) from None


# Per-note body write cap (spec `## Encoding + size`).
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# Topic description is also capped at the same limit to keep reads reasonable.
MAX_TOPIC_BYTES = MAX_BODY_BYTES

_TOPIC_PREVIEW_LEN = 80

# Regex to popular ATX-ish open fences handled in sections.py — reused only
# where this module parses bodies (nowhere). Kept out for now.

# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #


class StoreError(Exception):
    """Structured store-layer error. Carried `code` (from spec error table)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Result dataclasses (server layer maps these to pydantic shapes)             #
# --------------------------------------------------------------------------- #


@dataclass
class TopicInfo:
    dir: str
    description: str
    mtime: float
    size: int


@dataclass
class NoteInfo:
    name: str
    dir: str
    body: str
    mtime: float
    size: int


@dataclass
class SectionResult:
    name: str
    dir: str
    heading: str
    level: int
    body: str | None  # None if no match


@dataclass
class LinesResult:
    name: str
    dir: str
    start: int
    end: int
    total_lines: int
    lines: str


@dataclass
class LsDirItem:
    name: str
    has_topic: bool
    topic_preview: str | None = None


@dataclass
class LsPostitItem:
    name: str
    mtime: float
    size: int


@dataclass
class LsNoteModeResult:
    name: str
    dir: str
    total_lines: int
    headings: list[Heading] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Path helpers                                                                #
# --------------------------------------------------------------------------- #


def _norm_dir(dir: str | None) -> str:
    return _normalize_dir(dir)


def _dir_path(root: Path, dir: str) -> Path:
    d = _normalize_dir(dir)  # caller normally pre-normalized; this is defensive
    if d == ROOT:
        return root
    # `root / d` for a multi-segment `d` is fine — d has no leading/trailing
    # slashes (normalize_dir guarantees).
    return root / d


def _note_path(root: Path, dir: str, name: str) -> Path:
    return _dir_path(root, dir) / (name + NOTE_SUFFIX)


def _topic_path(root: Path, dir: str) -> Path:
    return _dir_path(root, dir) / TOPIC_FILENAME


def _is_topic_dir(root: Path, dir: str) -> bool:
    """True iff `dir` is a non-root dir that contains a `TOPIC.md`."""
    if dir == ROOT:
        return False
    p = _topic_path(root, dir)
    return p.is_file()


def _parent_dir(dir: str) -> str:
    """Normalized parent dir. For root-level dirs, parent is ROOT ('')."""
    if dir == ROOT:
        return ROOT
    parts = dir.split("/")
    parts.pop()
    return "/".join(parts)


# --------------------------------------------------------------------------- #
# Atomic writes                                                               #
# --------------------------------------------------------------------------- #


def _atomic_write_text(path: Path, data: str) -> None:
    """Write `data` (UTF-8, no BOM) atomically to `path`.

    Uses a tmp file in the *same directory* + `os.replace` to keep the
    rename atomic at FS level. Tmp is cleaned on failure.
    """
    tmp = path.with_name("." + path.name + ".agentpostit.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _stat_size_mtime(path: Path) -> tuple[int, float]:
    st = path.stat()
    return st.st_size, st.st_mtime


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Topic verbs                                                                 #
# --------------------------------------------------------------------------- #


def topic_create(root: Path, dir: str | None, description: str) -> TopicInfo:
    d = _norm_dir(dir)
    if d == ROOT:
        raise StoreError("invalid_path", "root is not a topic; create subdirs only")
    if not isinstance(description, str):
        raise StoreError("invalid_path", "description must be a string")
    if _byte_len(description) > MAX_TOPIC_BYTES:
        raise StoreError("too_large", "topic description exceeds 1 MiB cap")

    parent = _parent_dir(d)
    if parent != ROOT and not _is_topic_dir(root, parent):
        raise StoreError(
            "dir_missing",
            f"parent topic {parent!r} does not exist; call topic.create on it first",
        )

    target_dir = _dir_path(root, d)
    if target_dir.exists():
        raise StoreError("dir_exists", f"dir {d!r} already exists")

    target_dir.mkdir(parents=False)
    # `parents=False` would fail if intermediate dirs are missing — but our
    # parent check above already rejected that case for nested topics. Defensive
    # in case caller tampered paths. Use mkdir without parents to honour the
    # "create one level at a time" rule.
    topic_path = _topic_path(root, d)
    _atomic_write_text(topic_path, description)
    size, mtime = _stat_size_mtime(topic_path)
    return TopicInfo(dir=d, description=description, mtime=mtime, size=size)


def topic_read(root: Path, dir: str | None) -> TopicInfo | None:
    d = _norm_dir(dir)
    tp = _topic_path(root, d)
    if not tp.is_file():
        return None
    body = _read_text(tp)
    size, mtime = _stat_size_mtime(tp)
    return TopicInfo(dir=d, description=body, mtime=mtime, size=size)


def topic_write(root: Path, dir: str | None, description: str) -> TopicInfo:
    d = _norm_dir(dir)
    if d == ROOT:
        raise StoreError("invalid_path", "root is not a topic")
    if not isinstance(description, str):
        raise StoreError("invalid_path", "description must be a string")
    if _byte_len(description) > MAX_TOPIC_BYTES:
        raise StoreError("too_large", "topic description exceeds 1 MiB cap")
    target_dir = _dir_path(root, d)
    if not target_dir.is_dir():
        raise StoreError("dir_missing", f"dir {d!r} does not exist; call topic.create first")
    tp = _topic_path(root, d)
    _atomic_write_text(tp, description)
    size, mtime = _stat_size_mtime(tp)
    return TopicInfo(dir=d, description=description, mtime=mtime, size=size)


# --------------------------------------------------------------------------- #
# Postit CRUD                                                                 #
# --------------------------------------------------------------------------- #


def _require_topic_for_write(root: Path, d: str) -> None:
    """Enforce `dir_missing` for `postit.create` when `dir` is not a topic.

    Root is exempt (writes to root always succeed)."""
    if d == ROOT:
        return
    if not _dir_path(root, d).is_dir():
        raise StoreError("dir_missing", f"dir {d!r} does not exist; call topic.create first")
    if not _is_topic_dir(root, d):
        raise StoreError(
            "dir_missing",
            f"dir {d!r} has no TOPIC.md; call topic.create first",
        )


def postit_create(root: Path, name: str, body: str, dir: str | None = None) -> NoteInfo:
    n = _validate_name(name)  # raises InvalidNameError (reserved_name / invalid_name)
    d = _norm_dir(dir)
    if not isinstance(body, str):
        raise StoreError("invalid_path", "body must be a string")
    if _byte_len(body) > MAX_BODY_BYTES:
        raise StoreError("too_large", "body exceeds 1 MiB cap")
    _require_topic_for_write(root, d)
    target = _note_path(root, d, n)
    if target.exists():
        raise StoreError("already_exists", f"note {n!r} already exists in {d!r}")
    _atomic_write_text(target, body)
    size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=body, mtime=mtime, size=size)


def postit_update_body(
    root: Path,
    name: str,
    content: str,
    dir: str | None = None,
    mode: str = "overwrite",
) -> NoteInfo:
    if mode not in ("append", "overwrite"):
        raise StoreError("invalid_path", f"mode must be append or overwrite (got {mode!r})")
    n = _validate_name(name)
    d = _norm_dir(dir)
    if not isinstance(content, str):
        raise StoreError("invalid_path", "content must be a string")
    target = _note_path(root, d, n)
    if not target.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    existing = _read_text(target) if target.stat().st_size > 0 else ""
    if mode == "overwrite":
        new_body = content
    else:
        if existing and not existing.endswith("\n"):
            new_body = existing + "\n" + content
        else:
            new_body = existing + content
    if _byte_len(new_body) > MAX_BODY_BYTES:
        raise StoreError("too_large", "resulting body exceeds 1 MiB cap")
    _atomic_write_text(target, new_body)
    size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=new_body, mtime=mtime, size=size)


def postit_rename(root: Path, name: str, new_name: str, dir: str | None = None) -> NoteInfo:
    n = _validate_name(name)
    new_n = _validate_name(new_name)
    d = _norm_dir(dir)
    src = _note_path(root, d, n)
    dst = _note_path(root, d, new_n)
    if not src.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    if new_n == n:
        raise StoreError("no_op", "new_name equals name; nothing to rename")
    if dst.exists():
        raise StoreError("already_exists", f"note {new_n!r} already exists in {d!r}")
    os.replace(src, dst)
    size, mtime = _stat_size_mtime(dst)
    return NoteInfo(name=new_n, dir=d, body=_read_text(dst), mtime=mtime, size=size)


def postit_delete(root: Path, name: str, dir: str | None = None) -> None:
    n = _validate_name(name)
    d = _norm_dir(dir)
    target = _note_path(root, d, n)
    if not target.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    os.remove(target)
    # Intentionally do NOT rmdir even if dir is now empty (locked decision).


def postit_read(root: Path, name: str, dir: str | None = None) -> NoteInfo:
    n = _validate_name(name)
    d = _norm_dir(dir)
    target = _note_path(root, d, n)
    if not target.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    body = _read_text(target)
    size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=body, mtime=mtime, size=size)


def postit_read_section(
    root: Path,
    name: str,
    heading: str,
    dir: str | None = None,
    level: int = 2,
) -> SectionResult:
    if not isinstance(heading, str):
        raise InvalidNameError("heading must be a string")  # borrowed code path
    if not isinstance(level, int) or level < 1 or level > 6:
        raise StoreError("invalid_path", "level must be an integer 1..6")
    n = _validate_name(name)
    d = _norm_dir(dir)
    target = _note_path(root, d, n)
    if not target.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    body = _read_text(target)
    out = read_section(body, heading, level=level)
    return SectionResult(name=n, dir=d, heading=heading, level=level, body=out)


def _split_keepends(body: str) -> list[str]:
    """Split body into lines keeping their `\\n` terminator.

    Empty body yields an empty list (matches spec: total_lines == 0 for empty
    file). A non-empty body with no trailing newline yields a final segment
    without `\\n`.
    """
    lines: list[str] = []
    if body == "":
        return lines
    i = 0
    n = len(body)
    while i < n:
        nl = body.find("\n", i)
        if nl == -1:
            lines.append(body[i:])
            break
        lines.append(body[i : nl + 1])
        i = nl + 1
    return lines


def postit_read_lines(
    root: Path,
    name: str,
    start: int,
    end: int,
    dir: str | None = None,
) -> LinesResult:
    if not isinstance(start, int) or not isinstance(end, int):
        raise StoreError("invalid_range", "start and end must be integers")
    if start < 1:
        raise StoreError("invalid_range", "start must be >= 1")
    if end < start:
        raise StoreError("invalid_range", "end must be >= start")
    n = _validate_name(name)
    d = _norm_dir(dir)
    target = _note_path(root, d, n)
    if not target.is_file():
        raise StoreError("not_found", f"note {n!r} not found in {d!r}")
    body = _read_text(target)
    lines = _split_keepends(body)
    total = len(lines)
    if total == 0:
        return LinesResult(name=n, dir=d, start=start, end=0, total_lines=0, lines="")
    end_clamped = min(end, total)
    if start > total:
        # start beyond EOF: clamp to empty result. Returned `end` reflects the
        # clamp boundary (== total), matching spec's "end reflects actual last
        # line read" — here, no lines read so end == ??? Use start-1 to signal
        # nothing was returned. We pick end == end_clamped for consistency.
        return LinesResult(name=n, dir=d, start=start, end=end_clamped, total_lines=total, lines="")
    selected = lines[start - 1 : end_clamped]
    return LinesResult(
        name=n,
        dir=d,
        start=start,
        end=end_clamped,
        total_lines=total,
        lines="".join(selected),
    )


# --------------------------------------------------------------------------- #
# `postit.ls`                                                                 #
# --------------------------------------------------------------------------- #


def _is_postit_filename(entry_name: str) -> bool:
    """True iff `entry_name` is a `.md` file basename that is NOT `TOPIC.md`."""
    if not entry_name.endswith(NOTE_SUFFIX):
        return False
    if entry_name == TOPIC_FILENAME:
        return False
    return True


def _topic_preview_of(topic_path: Path) -> str | None:
    if not topic_path.is_file():
        return None
    try:
        with open(topic_path, "r", encoding="utf-8", newline="") as f:
            chunk = f.read(_TOPIC_PREVIEW_LEN)
    except OSError:
        return None
    return chunk


def postit_ls(
    root: Path,
    dir: str | None = None,
    name: str | None = None,
    recursive: bool = False,
):
    d = _norm_dir(dir)

    # --- Note mode -----------------------------------------------------------
    if name is not None:
        n = _validate_name(name)
        target = _note_path(root, d, n)
        if not target.is_file():
            raise StoreError("not_found", f"note {n!r} not found in {d!r}")
        body = _read_text(target)
        hs = parse_headings(body)
        total = len(_split_keepends(body))
        return LsNoteModeResult(name=n, dir=d, total_lines=total, headings=hs)

    # --- Dir mode ------------------------------------------------------------
    start_dir = _dir_path(root, d)
    if not start_dir.is_dir():
        # Nonexistent / not-yet-created dir: empty listing rather than error
        # (no `dir_missing` code defined for `ls`; spec implicitly treats it
        # as "nothing here"). This also covers root before any note exists.
        return []

    items: list[tuple[str, object]] = []

    if not recursive:
        for entry in os.scandir(start_dir):
            if entry.is_dir(follow_symlinks=False):
                rel_name = entry.name
                tp = Path(entry.path) / TOPIC_FILENAME
                has_topic = tp.is_file()
                preview = _topic_preview_of(tp) if has_topic else None
                items.append(
                    (
                        rel_name,
                        LsDirItem(name=rel_name, has_topic=has_topic, topic_preview=preview),
                    )
                )
            elif entry.is_file(follow_symlinks=False) and _is_postit_filename(entry.name):
                rel_name = entry.name[: -len(NOTE_SUFFIX)]
                st = entry.stat(follow_symlinks=False)
                items.append(
                    (
                        rel_name,
                        LsPostitItem(name=rel_name, mtime=st.st_mtime, size=st.st_size),
                    )
                )
            # Foreign files silently ignored.
    else:
        # Walk subtree rooted at `start_dir`. `os.walk` yields (dirpath, dirnames,
        # filenames) for the start dir and every descendant. We list postits as
        # direct children of each walked dir, and subdirs as their own entries.
        for dirpath, dirnames, filenames in os.walk(start_dir):
            base = Path(dirpath)
            for sub in dirnames:
                full = base / sub
                tp = full / TOPIC_FILENAME
                has_topic = tp.is_file()
                preview = _topic_preview_of(tp) if has_topic else None
                rel = _rel_to_start(start_dir, full)
                items.append(
                    (
                        rel,
                        LsDirItem(name=rel, has_topic=has_topic, topic_preview=preview),
                    )
                )
            for fn in filenames:
                if not _is_postit_filename(fn):
                    continue
                full = base / fn
                rel_name_root = full.stem  # filename minus .md
                rel = _rel_to_start(start_dir, full)
                st = full.stat()
                items.append(
                    (
                        rel,
                        LsPostitItem(
                            name=_postit_rel_name(start_dir, full, rel),
                            mtime=st.st_mtime,
                            size=st.st_size,
                        ),
                    )
                )
            # `os.walk` default is top-down; we don't prune —
            # foreign subdirs (no TOPIC.md) are still listed & recursed.

    # Sort by sort key (byte order). Items: (sort_key, item).
    items.sort(key=lambda it: it[0].encode("utf-8"))
    return [it for _, it in items]


def _rel_to_start(start: Path, path: Path) -> str:
    """Relative path from `start` to `path`, joined with `/`. Empty for `start`
    itself (which we never emit, but defensible)."""
    rel = path.relative_to(start)
    return "/".join(rel.parts)


def _postit_rel_name(start: Path, full: Path, rel: str) -> str:
    """For recursive dir mode: postit `name` = relative path (slash-joined)
    with the `.md` stripped off.

    I.e. `remember-me/foo.md` -> `remember-me/foo`.
    """
    if rel.endswith(NOTE_SUFFIX):
        return rel[: -len(NOTE_SUFFIX)]
    return rel