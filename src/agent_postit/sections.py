"""Markdown heading parsing + section extraction.

Spec: README.md `## Section semantics` + `#### Heading parser`.
ATX-only, fence-aware (``` and ~~~ of equal/greater length), setext lines
treated as body, close-form `## foo ##` -> text `foo`. Indented (4-space)
code blocks are NOT special-cased.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Closing-form heading: capture content up to the last non-space char,
# require a whitespace run, then a trailing run of `#`.
_CLOSE_FORM_RE = re.compile(r"^(.*?\S)\s+#+$")

# Opening fence: 0..3 leading spaces, then a run of >= 3 identical fence
# chars (``` or ~~~).
_OPEN_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# Closing fence: 0..3 leading spaces, then a run of fence chars >= the
# opening length, then only whitespace to end of line.
def _close_fence_re(char: str, length: int) -> re.Pattern[str]:
    return re.compile(rf"^ {{0,3}}{re.escape(char)}{{{length},}}\s*$")


@dataclass(frozen=True)
class Heading:
    """A parsed ATX heading.

    Attributes:
        level: 1..6.
        heading: text content (close-form `#` stripped, whitespace trimmed).
        line_no: 1-based line number in the body.
        offset: 0-based character offset into the body where the heading
            line begins (used for byte-accurate section slicing).
    """

    level: int
    heading: str
    line_no: int
    offset: int


def _parse_atx_heading(line: str) -> tuple[int, str] | None:
    """Try to parse `line` as an ATX heading. Return `(level, text)` or `None`.

    Rules (per spec):
    - 0..3 leading spaces; a 4th space disqualifies (indented code block).
    - 1..6 `#` characters.
    - At least one space after the `#`-run.
    - Text = rest, with optional trailing close-form `#+` (preceded by
      whitespace) stripped, then surrounding whitespace trimmed.
    """
    indent = 0
    while indent < len(line) and line[indent] == " " and indent < 4:
        indent += 1
    if indent == 4:
        return None
    # Must reach a `#`.
    if indent >= len(line) or line[indent] != "#":
        return None
    j = indent
    while j < len(line) and line[j] == "#":
        j += 1
    count = j - indent
    if count < 1 or count > 6:
        return None
    # Required space between # run and text.
    if j >= len(line) or line[j] != " ":
        return None
    rest = line[j + 1 :]
    text = rest.strip()
    m = _CLOSE_FORM_RE.match(text)
    if m:
        text = m.group(1)
    return count, text


def parse_headings(body: str) -> list[Heading]:
    """Parse all ATX headings in `body`, skipping fenced code blocks.

    Setext underlines (`Foo\\n===`) are NOT headings — the underline line
    is plain body.
    """
    return _parse_clean(body)


def _parse_clean(body: str) -> list[Heading]:
    headings: list[Heading] = []
    fence_char: str | None = None
    fence_len: int = 0

    # Track byte offset of each line's start.
    offset = 0
    line_no = 0
    # Splitting Python's str.splitlines() drops the line terminators but
    # also mis-handles trailing-newline-only; we iterate manually to keep
    # offsets exact and to detect the trailing line case.
    idx = 0
    n = len(body)
    while idx <= n:
        nl = body.find("\n", idx)
        if nl == -1:
            line_end = n
            line = body[idx:n]
            advance = False  # last iteration
        else:
            line_end = nl
            line = body[idx:nl]
            advance = True
        offset = idx
        line_no += 1

        # Fence handling.
        if fence_char is not None:
            close_re = _close_fence_re(fence_char, fence_len)
            if close_re.match(line):
                fence_char = None
                fence_len = 0
        else:
            m = _OPEN_FENCE_RE.match(line)
            if m:
                fence_char = "`" if "`" in m.group(2) else "~"
                fence_len = m.group(2).count(fence_char)
            else:
                parsed = _parse_atx_heading(line)
                if parsed is not None:
                    level, text = parsed
                    headings.append(
                        Heading(level=level, heading=text, line_no=line_no, offset=offset)
                    )

        if not advance:
            break
        idx = nl + 1

    return headings


def read_section(body: str, heading: str, level: int = 2) -> str | None:
    """Return the slice of `body` from the first ATX heading whose text
    equals `heading` (case-insensitive) at exactly `level`, through the next
    heading of level <= `level` (or EOF).

    Includes the matched heading line and all sub-headers + their content
    verbatim. Returns `None` if no heading matches.
    """
    if level < 1 or level > 6:
        # Out-of-range level can never match a parsed (1..6) heading.
        return None
    target_norm = heading.casefold()
    headings = _parse_clean(body)
    match_idx: int | None = None
    for idx, h in enumerate(headings):
        if h.level == level and h.heading.casefold() == target_norm:
            match_idx = idx
            break
    if match_idx is None:
        return None
    start_offset = headings[match_idx].offset
    # Find the offset where the next heading of level <= our level begins.
    end_offset = len(body)
    for h in headings[match_idx + 1 :]:
        if h.level <= level:
            end_offset = h.offset
            break
    return body[start_offset:end_offset]