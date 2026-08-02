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
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .paths import (
    NOTE_SUFFIX,
    ROOT,
    TOPIC_BASENAME,
    TOPIC_FILENAME,
    InvalidNameError,
    InvalidPathError,
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
# Per-note write lock (single-process)                                         #
# --------------------------------------------------------------------------- #
#
# HTTP transport means many concurrent tool calls against the same root.
# Per-note integrity is already protected by `_atomic_write_text` (tmp +
# fsync + os.replace), but multi-step ops (`postit.append` is
# read-modify-write, `postit.rename` touches two names, `postit.create`
# has a check-then-write race) need a process-wide critical section. We
# use `threading.RLock` keyed by `f"{normalized_dir}/{lowercase_name}"`;
# reads are lock-free (atomic replace guarantees they see a complete
# file, old or new). Single-instance service → one process owns all
# locks; no cross-process locking is attempted.

_NOTE_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.RLock:
    """Get-or-create the `RLock` for a single note identity.

    Locks are created lazily and never evicted — see §4.5. Names are short
    and bounded by the 1 MiB body cap on note creation, so unbounded growth
    is not a real concern; an LRU cap can be added later if it ever is.
    """
    with _LOCKS_GUARD:
        lk = _NOTE_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _NOTE_LOCKS[key] = lk
        return lk


@contextmanager
def _with_note_locks(*keys: str):
    """Acquire one or more note locks in a stable order.

    Keys are acquired in sorted order to avoid self-deadlock (e.g. two
    concurrent renames `A→B` and `B→A`). All locks are reentrant so a
    tool that legitimately re-enters its own critical section doesn't
    deadlock.
    """
    ordered = sorted(set(keys))
    with ExitStack() as stack:
        for key in ordered:
            stack.enter_context(_lock_for(key))
        yield


def _note_lock_key(dir_: str, name: str) -> str:
    """Lock key for a postit. `dir_` must already be normalized; `name` must
    already be validated (lowercased). Root dir is `''` so a root note
    `foo` locks on `'/foo'` — joining with `/` keeps it visually distinct
    from a topic note `topic/foo` → `'topic/foo'`. Strings are compared
    as plain bytes; the leading slash on root is fine because topic
    paths never start with one (normalize_dir strips it)."""
    if dir_ == ROOT:
        return f"/{name}"
    return f"{dir_}/{name}"


def _topic_lock_key(dir_: str) -> str:
    """Lock key for a topic's `TOPIC.md`. Topic `TOPIC` is the reserved
    basename in uppercase; a postit note never locks on this key because
    `validate_name` rejects `TOPIC`/`topic`/etc. case-insensitively."""
    if dir_ == ROOT:
        # Root has no TOPIC.md, but the key is never acquired for root —
        # topic verbs refuse root before locking. Defend against a coding
        # mistake by giving root a non-colliding key anyway.
        return "/TOPIC"
    return f"{dir_}/{TOPIC_BASENAME}"

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


def _fsync_fd(fd: int) -> None:
    """Best-effort `fsync` on an open fd. Silently ignore unsupported FS."""
    try:
        os.fsync(fd)
    except OSError:
        # EINVAL / ENOTSUP / EBADF on weird filesystems (tmpfs, network
        # mounts, /dev/null, etc.). Durability is a best-effort property
        # here; the rename remains atomic regardless.
        pass


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of the directory holding `path`'s dir entry.

    Required to durably commit a rename / unlink on POSIX: the file's data
    may be flushed by fsync on the file fd, but the directory entry update
    is only committed by fsync on the parent dir fd.
    """
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        _fsync_fd(dir_fd)
    finally:
        os.close(dir_fd)


def _atomic_write_text(path: Path, data: str) -> None:
    """Write `data` (UTF-8, no BOM) atomically and durably to `path`.

    Sequence: write tmp → fsync tmp → `os.replace` → fsync parent dir.
    This is the canonical POSIX crash-safe write:
      - the rename is atomic at FS level so readers always see either
        the old or the new file, never a half-written body;
      - fsync of the tmp fd persists the bytes before the rename;
      - fsync of the parent dir persists the directory entry update so
        the rename survives power loss immediately after this call.
    On any exception the tmp file is unlinked (best-effort) and re-raised.
    """
    tmp = path.with_name("." + path.name + ".agentpostit.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            _fsync_fd(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path)
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

    # Serialize concurrent `topic.create` on the same dir name: a mkdir
    # check-then-create race would otherwise let two callers both pass
    # the `target_dir.exists()` check and both attempt `mkdir`, one of
    # which would raise `FileExistsError` instead of our `dir_exists`.
    with _with_note_locks(_topic_lock_key(d)):
        target_dir = _dir_path(root, d)
        if target_dir.exists():
            # Idempotence: a repeat `topic.create` with the exact same
            # `dir` + `description` byte-matching the existing `TOPIC.md`
            # body is a no-op success. This makes `topic.create` safe to
            # retry by hosts that use the `idempotentHint` annotation.
            # Any divergence (no `TOPIC.md`, or body differs) → `dir_exists`.
            tp = _topic_path(root, d)
            if not tp.is_file():
                raise StoreError("dir_exists", f"dir {d!r} already exists without TOPIC.md")
            existing = _read_text(tp)
            if existing == description:
                size, mtime = _stat_size_mtime(tp)
                return TopicInfo(dir=d, description=description, mtime=mtime, size=size)
            raise StoreError("dir_exists", f"dir {d!r} already exists with a different description")

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
    with _with_note_locks(_topic_lock_key(d)):
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
    # Hold the per-note lock across the check-then-write so two concurrent
    # `postit.create` of the same name can't both pass the existence check
    # and both succeed — exactly one wins, the rest get `already_exists`.
    with _with_note_locks(_note_lock_key(d, n)):
        if target.exists():
            raise StoreError("already_exists", f"note {n!r} already exists in {d!r}")
        _atomic_write_text(target, body)
        size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=body, mtime=mtime, size=size)


def postit_append(
    root: Path,
    name: str,
    content: str,
    dir: str | None = None,
) -> NoteInfo:
    """Append `content` to a note's body (read-modify-write under the
    per-note lock).

    Without the lock two concurrent appenders would both read the same
    body, both append, and one would lose its append on the losing
    `os.replace`. The lock makes appends serial w.r.t. each other and
    w.r.t. create/rename/delete on the same identity.
    """
    n = _validate_name(name)
    d = _norm_dir(dir)
    if not isinstance(content, str):
        raise StoreError("invalid_path", "content must be a string")
    target = _note_path(root, d, n)
    with _with_note_locks(_note_lock_key(d, n)):
        if not target.is_file():
            raise StoreError("not_found", f"note {n!r} not found in {d!r}")
        existing = _read_text(target) if target.stat().st_size > 0 else ""
        if existing and not existing.endswith("\n"):
            new_body = existing + "\n" + content
        else:
            new_body = existing + content
        if _byte_len(new_body) > MAX_BODY_BYTES:
            raise StoreError("too_large", "resulting body exceeds 1 MiB cap")
        _atomic_write_text(target, new_body)
        size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=new_body, mtime=mtime, size=size)


def postit_overwrite(
    root: Path,
    name: str,
    content: str,
    dir: str | None = None,
) -> NoteInfo:
    """Replace a note's body with `content` (atomic).

    Held under the per-note lock so overwrites are serial w.r.t.
    concurrent create/append/rename/delete on the same identity.
    """
    n = _validate_name(name)
    d = _norm_dir(dir)
    if not isinstance(content, str):
        raise StoreError("invalid_path", "content must be a string")
    target = _note_path(root, d, n)
    with _with_note_locks(_note_lock_key(d, n)):
        if not target.is_file():
            raise StoreError("not_found", f"note {n!r} not found in {d!r}")
        if _byte_len(content) > MAX_BODY_BYTES:
            raise StoreError("too_large", "resulting body exceeds 1 MiB cap")
        _atomic_write_text(target, content)
        size, mtime = _stat_size_mtime(target)
    return NoteInfo(name=n, dir=d, body=content, mtime=mtime, size=size)


def postit_rename(root: Path, name: str, new_name: str, dir: str | None = None) -> NoteInfo:
    n = _validate_name(name)
    new_n = _validate_name(new_name)
    d = _norm_dir(dir)
    src = _note_path(root, d, n)
    dst = _note_path(root, d, new_n)
    # Acquire both src and dst note locks in sorted order so two
    # simultaneous renames `A→B` and `B→A` can't self-deadlock. The
    # `no_op` (src == dst) and `already_exists` (dst present) checks both
    # run inside the critical section so the rename is atomic w.r.t.
    # concurrent `create`/`append`/`delete` on either name.
    with _with_note_locks(_note_lock_key(d, n), _note_lock_key(d, new_n)):
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
    with _with_note_locks(_note_lock_key(d, n)):
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
    """True iff `entry_name` is a lowercase `.md` file that is NOT a topic marker.

    Rule (case-insensitive on the *topic* aspect only):
    - The `.md` suffix match is **case-sensitive**: we only ever write
      lowercase `.md`, so a hand-created `Foo.MD` / `Foo.Md` on disk is
      treated as foreign and skipped (matches the foreign-file spec).
    - The `TOPIC.md` skip is **case-insensitive**: a stray `Topic.md` /
      `topic.md` is still the reserved marker, not a postit.
    """
    if not entry_name.endswith(NOTE_SUFFIX):
        return False
    return entry_name.lower() != TOPIC_FILENAME.lower()


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