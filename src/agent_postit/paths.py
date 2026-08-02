"""Path normalization + note-name validation (server-side, always applied).

Spec: README.md `## Path normalization` and `## Note-name validation`.

These helpers operate on user-supplied `dir` / `name` strings BEFORE any
filesystem op. They either return a clean value or raise `InvalidPathError` /
`InvalidNameError` (which the server layer translates to a `ToolError` with
codes `invalid_path` / `invalid_name`).
"""

from __future__ import annotations

# Reserved topic-marker basename (no postit may be named this in any dir).
TOPIC_BASENAME = "TOPIC"
TOPIC_FILENAME = "TOPIC.md"
NOTE_SUFFIX = ".md"

# Root sentinel: normalize_dir returns "" for the root dir. The store layer
# treats "" as `POSTIT_ROOT` itself; any other value is a sub-path relative
# to the root.
ROOT = ""


class PathError(Exception):
    """Base for path/name validation errors."""

    code: str = "invalid_path"
    message: str = "invalid path"


class InvalidPathError(PathError):
    code = "invalid_path"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidNameError(PathError):
    code = "invalid_name"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ReservedNameError(InvalidNameError):
    code = "reserved_name"


def normalize_dir(dir: str) -> str:
    """Normalize a user-supplied dir argument.

    - `""`, `"."`, `"/"` -> root sentinel (`""`).
    - Strip trailing `/`, strip leading `/`.
    - Reject any `\\0` anywhere in the input.
    - Reject any component equal to `..` (escape attempt).
    - Reject empty intermediate components (e.g. `"a//b"` — collapsed silently
      would hide intent; reject to surface caller bugs).
    - **All components are lowercased on the way through.** Names on disk are
      always lowercase; callers may pass any case and it will be folded.

    The returned value is a POSIX relative path (or `""` for root), with no
    leading or trailing slash, fully lowercased.
    """
    if dir is None:
        # Defensive: treat None as root. (Tool layer should never pass None
        # to normalize_dir — it substitutes its own default first.)
        return ROOT
    if not isinstance(dir, str):
        raise InvalidPathError("dir must be a string")

    if "\0" in dir:
        raise InvalidPathError("dir contains NUL byte")

    # Special canonical roots (before stripping) — match spec exactly.
    if dir in ("", ".", "./", "/"):
        return ROOT
    # "./." and "/." also collapse to root; cover the obvious variants.
    if dir.rstrip("/") in ("", "."):
        return ROOT

    s = dir
    # Strip a single leading slash; "/foo" is treated as the relative path
    # "foo" (per spec test `normalize("/foo") -> "foo"`). We do not reject
    # leading-slash inputs; the reject-absolute rule is moot on POSIX once
    # we've already escaped `..`.
    s = s.removeprefix("/")
    # Strip trailing slashes until none remain.
    while s.endswith("/"):
        s = s[:-1]
    if s == "" or s == ".":
        return ROOT

    components: list[str] = []
    for part in s.split("/"):
        if part == "":
            # Adjacent slashes — collapse, do not reject. Keeps the layer
            # forgiving for `"foo//bar"` callers (they meant `"foo/bar"`).
            continue
        if part == "..":
            raise InvalidPathError("dir contains '..' component (escape attempt)")
        if part == ".":
            # Skip explicit `.` components ("./a" == "a").
            continue
        if "\0" in part:
            # `re` split handles embedded NUL — only reachable when "split"
            # did not already reject via the whole-string check above; kept
            # for defence in depth.
            raise InvalidPathError("dir component contains NUL byte")
        components.append(part.lower())

    if not components:
        return ROOT
    return "/".join(components)


def dir_components(dir: str) -> list[str]:
    """Split an already-normalized dir into path components (no slash).

    Root (`""`) yields `[]`.
    """
    if dir == ROOT:
        return []
    return dir.split("/")


def validate_name(name: str) -> str:
    """Validate a note name. Returns the name **lowercased** (no slugification).

    Rejects:
    - empty / not-a-str
    - contains `/`, `\\0`, newline (`\\n` or `\\r\\n`)
    - begins with `.` (dotfile confusion)
    - equals reserved `topic` (any case — `TOPIC.md` is the topic marker
      file; `topic`, `Topic`, `TOPIC` are all rejected because the input is
      lowercased before the reserved check, and the on-disk filename is
      case-folded too)

    Caller is responsible for appending `.md`.
    """
    if not isinstance(name, str):
        raise InvalidNameError("name must be a string")
    if name == "":
        raise InvalidNameError("name is empty")
    folded = name.lower()
    if folded == TOPIC_BASENAME.lower():
        raise ReservedNameError("name is reserved (TOPIC.md marker)")
    if folded.startswith("."):
        raise InvalidNameError("name begins with '.' (dotfile confusion)")
    if "/" in folded:
        raise InvalidNameError("name contains '/'")
    if "\0" in folded:
        raise InvalidNameError("name contains NUL byte")
    if "\n" in folded or "\r" in folded:
        raise InvalidNameError("name contains a newline")
    return folded


def note_filename(name: str) -> str:
    """Filename for a validated note name: `name + ".md"`."""
    return name + NOTE_SUFFIX