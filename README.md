# agent-postit

Local MCP (Model Context Protocol) server giving agents persistent
"post-it" memory — tidbits, notes, reminders — like a human's sticker board,
stored on the **native filesystem** as plain `.md` files. Reach over
**stdio**, **no auth** (any local process that spawns the server can CRUD).

> Goal: let an agent store and recall small pieces of information across
> sessions, the way a person scribbles a sticky note to remember something
> later. Each "topic" lives in its own directory, described by a `TOPIC.md`.
> Generic notes (no topic) live at the root.

---

## Status

**Planning phase.** Nothing built yet. This file is the spec — read it, build
from it, update it as decisions get made or changed.

---

## Decisions (locked)

| Concern      | Choice                                                             |
|--------------|--------------------------------------------------------------------|
| Name         | **agent-postit** (project), `agent_postit` (import), `agent-postit` (MCP server advertised name) |
| Transport    | **stdio** (local, one agent per process invocation)                |
| Language     | **Python 3.12+** (matches `python:3.12-slim` base)             |
| MCP SDK      | **FastMCP** (`mcp` PyPI package)                                   |
| Packaging    | **uv** — `pyproject.toml` + `uv.lock`, `uv sync` to install         |
| Storage      | **Native filesystem.** Notes = `.md` files. No DB.                 |
| Layout       | Root dir holds generic notes; subdirs are **topics**, each with a `TOPIC.md` |
| Topic creation | **Explicit.** `topic.create` makes dir + `TOPIC.md`. `postit.create` refuses if dir missing. |
| Empty dir after last note delete | **Keep.** Directories may be empty. `TOPIC.md` survives. |
| Auth         | **None.** Trust boundary = whoever can spawn the process.         |
| Pinning      | **Not in v1.** Recall via `recent` + `ls` + `search`. Add later if needed. |
| Concurrency  | **Single stdio process assumed.** No file locking. Multi-process (podman) noted as known limit. |

### Filesystem layout

```
$POSTIT_ROOT/                       (env var; default ~/.agent-postit)
├── deploy-k8s.md                   generic note (no directory)
├── weekend.md                      generic note
├── remember-me/                     a topic
│   ├── TOPIC.md                     "this dir is about the remember-me project"
│   ├── schema-ideas.md             a postit in this topic
│   └── decisions.md
└── personal/
    ├── TOPIC.md
    └── dentist-appt.md
```

- Root dir = generic notes; **no `TOPIC.md`** at root (root is not a topic).
- Each subdir = one topic. Subdir **must** contain a `TOPIC.md` describing
  what the topic is about. Created by `topic.create` only — never auto-created
  by `postit.create`.
- `TOPIC.md` description content is not validated or enforced. Convention
  strongly encouraged (good practice), not a hard requirement.
- Notes are plain `.md` files. Filename (minus `.md`) **is** the note name,
  kept **verbatim** — no slugification. Agent responsibility for FS-safe chars.
- `TOPIC.md` is reserved: no postit may be named `TOPIC` in any dir.
- Subdirs may nest (topic within topic); each nested subdir gets its own
  `TOPIC.md` via its own `topic.create`.

### Note-file format

```markdown
<.body — free-form markdown, possibly with ## sections>
```

No frontmatter, no H1. The **filename is the title**. Body is whatever is in
the file. Sections = `## H2` headings in the body (see `read_section`).

### Path normalization (server-side, always applied)

`dir` arguments are normalized before any filesystem op:

- `""` or `"."` or `"/"` → root.
- Strip trailing `/`.
- Strip leading `/`.
- **Reject** any path with `..` components (escape attempt).
- **Reject** absolute paths.
- **Reject** any component containing `\0` or `/`.
- Case-sensitive on Linux (do not normalize case).
- **Caveat (case-insensitive FS).** On macOS hosts running under
  podman, or any filesystem mounted case-insensitive, `Foo.md` and
  `foo.md` collide and the second write silently overwrites the first.
  No normalization in v1 — document and accept the risk; run on a
  case-sensitive root dir when collisions matter.

### Note-name validation (server-side, always applied)

`name` arguments are validated:

- **Reject** if contains `/`, `\0`, or newline.
- **Reject** if begins with `.` (dotfile confusion).
- **Reject** if equals `TOPIC` (reserved).
- **Reject** if empty.
- Otherwise verbatim — appended `.md`, written as-is.

### Encoding + size

- All files UTF-8, no BOM. CRLF not normalized (stored verbatim).
- Body write size cap: **1 MiB** per `create` / `update_body` call. Larger is
  refused with code `too_large`. (No total-tree cap in v1.)

### Foreign files (non-postit) ignored

`ls`, `search`, `recent` **only** see `*.md` excluding `TOPIC.md`. Any other
file in a topic dir (stray `.txt`, `.swp`, editor backups, etc.) is silently
ignored, never listed or matched. Tool set is disjoint:

- `topic.*` verbs operate on `TOPIC.md` only.
- `postit.*` verbs operate on every other `*.md` file.
- `postit.read(name="TOPIC")` → error `reserved_name`.

### Topic files never searched / never listed as postits

`search` and `recent` skip `TOPIC.md`. To read or update a topic
description, use `topic.read` / `topic.write`. There is no `topic.search`.

---

## MCP tools (verbs)

Path addressing = `(dir, name)` instead of integer IDs (no DB → no IDs).
`dir` defaults to root (`.`). `name` is the filename without `.md`.

### Topic verbs (operate on dirs + `TOPIC.md`)

| Tool              | Args                                              | Behavior                                                            |
|-------------------|---------------------------------------------------|---------------------------------------------------------------------|
`dir` (req), `description` (req, may be `""` — produces a zero-length `TOPIC.md`, allowed) | Make dir at normalized `dir`, write `TOPIC.md` with `description` as body (atomic). **Refuse if `dir` already exists** (whether empty, foreign, or already a topic) → `dir_exists`. No adoption of foreign dirs in v1 — keeps the topic axis explicit; onboard stray dirs by deleting them first or by `mv`-ing their contents into a freshly-created topic. **Parent must already be a topic** (or `dir` lives at root): creating `/a/b` requires `/a` to already have a `TOPIC.md`; otherwise refuse with `dir_missing` and hint to `topic.create` the parent first. This is the **only** way to make a new topic — nested topics created one level at a time, top-down.
| `topic.read`      | `dir` (req)                                       | Return `TOPIC.md` body for the given dir. `null` if dir missing or topic file missing. |
| `topic.write`     | `dir` (req), `description` (req, overwrite-only)  | Overwrite `TOPIC.md`. **Refuse if dir missing** (caller must `topic.create` first). |

### Postit CRUD

| Tool              | Args                                              | Behavior                                                            |
|-------------------|---------------------------------------------------|---------------------------------------------------------------------|
| `postit.create`   | `name` (req), `body` (req, may be `""`), `dir?` (default root) | Write `<dir>/<name>.md` atomically (tmp + `os.replace`). **Refuse if dir missing** → error `dir_missing` with hint to call `topic.create` first. **Refuse if file exists** → `already_exists`. Empty `body` allowed — produces a zero-length `.md` file. |
| `postit.update_body` | `name` (req), `dir?`, `mode` (`append`\|`overwrite`, default `overwrite`), `content` (req) | `overwrite` writes `content` atomically. `append` reads existing body, concatenates `content` (with a trailing newline if missing on existing body), writes back atomically. Bumps mtime via the write. **Refuse if note missing** → `not_found`. Size cap applies to resulting body. |
| `postit.rename`   | `name` (req), `dir?`, `new_name` (req)             | `os.replace(<dir>/<name>.md, <dir>/<new_name>.md)`. **Refuse** if `new_name == name` → `no_op`. **Refuse** if `<new_name>.md` exists → `already_exists`. **Same-dir only** (cross-dir move out of scope for v1). `new_name` runs through note-name validation. |
| `postit.delete`   | `name` (req), `dir?`                               | `rm <dir>/<name>.md`. Does **not** remove dir, even if now empty (per locked decision). Idempotent re-delete → `not_found`. |
| `postit.read`      | `name` (req), `dir?`                               | Return `{name, dir, body, mtime, size}`. Body may be large — caller may prefer `read_section` / `read_lines` if it only wants part. `not_found` if missing. |
| `postit.read_section` | `name` (req), `dir?`, `heading` (req, case-insensitive exact match), `level?` (default `2`, accepts 1–6) | Return body text starting at the first heading whose text equals `heading` (case-insensitive) at the given `level`. Includes the matched heading line and **everything under it until the next heading of level ≤ `level` or EOF** — i.e. all subheaders and their content are included verbatim. `null` if no match. See **Section semantics** below. |
| `postit.read_lines` | `name` (req), `dir?`, `start` (req, 1-based), `end` (req, inclusive) | Return `{name, dir, start, end, total_lines, lines}` where `lines` is the joined text of body lines `start..end` (inclusive, 1-based). Bounds: `start ≥ 1`, `end ≥ start` else `invalid_range`. Beyond EOF → clamped (returns what exists). Empty file → `lines: ""`. |

### High-level

| Tool              | Args                                              | Behavior                                                            |
|-------------------|---------------------------------------------------|---------------------------------------------------------------------|
| `postit.ls`       | `dir?` (default root), `name?`, `recursive?` (default `false`, only used in dir mode) | **Two modes.** (a) **Dir mode** (`name` absent): single flat `ls -la`-style array of items in `dir`, dirs and postits **interleaved alphabetically** (truer to `ls -la` — no dirs-first block): `[{type: "dir", name, has_topic, topic_preview?}, {type: "postit", name, mtime, size}]`. Sort key = `name` ascending (byte order, case-sensitive per the FS caveat). `recursive=true` walks entire subtree under `dir`, still one flat array with full relative paths in `name`; in recursive mode the sort key is the full relative path (depth-stable alphabetical, i.e. `remember-me/foo.md` sorts before `remember-me2.md` because `/` < `2` in byte order — acceptable, predictable). Foreign files ignored. `TOPIC.md` not listed. (b) **Note mode** (`name` set): return `{name, dir, total_lines, headings: [{level, heading, line_no}]}` — list of markdown headings in the file in document order, with their 1-based line numbers; **no body content returned**. `recursive` is ignored in note mode. `name="TOPIC"` → `reserved_name`. `not_found` if note missing. Empty `headings` array if note has no headings. Use this to TOC a note before deciding what to `read_section` or `read_lines`. |
| `postit.search`   | `pattern` (Python `re` regex), `scope` (`name`\|`body`\|`both`, default `both`), `dir?` (default root), `recursive?` (default `true`), `limit?` (default 50) | Walk subtree, `re.search(pattern, ...)` across names and/or bodies (case-insensitive unless `(?-i)`). Return matches: `[{path, name, body_matches: [{line_no, line}], name_match: bool}]`. **Full lines containing match returned** (grep-like). Empty-body notes match on name when `scope` includes `name`. Caps at `limit`. Skips `TOPIC.md`. |
`limit?` (default 10), `dir?` (default root) | Walk the **subtree** rooted at `dir` (always recursive — no `recursive` or `all_topics` flag, the tool always descends so nested-topic postits surface when you point it at a parent). Sort postits by mtime desc (tiebreaker: `path` ascending), return top `limit` as `[{path, name, mtime, size}]` (no body). Default `dir=root` → every postit everywhere (root's subtree = whole tree). To scope, set `dir=<topic>`; that already descends into its nested topics. No opt-out of recursion in `recent` — if you want only that exact dir, use `postit.ls` (dir mode) + `postit.read`. The session-start recall tool. **Body preview deliberately not included** — caller reads selectively via `postit.read` / `read_section` to keep `recent` cheap.

### Section semantics (`read_section`)

Given body:

```
## Setup
do X
### Sub-step
foo
## Notes
bar
```

- `read_section(name, heading="Setup")` (default `level=2`, case-insensitive
  exact text match) → returns everything starting at `## Setup` until the next
  heading of level ≤ 2 (`## Notes`) or EOF. **Includes the matched heading
  line and all subheaders + their content verbatim:**
  ```
  ## Setup
  do X
  ### Sub-step
  foo
  ```
- `read_section(name, heading="setup")` → same result (case-insensitive).
- `read_section(name, heading="SETUP")` → same result.
- `read_section(name, heading="Sub-step", level=3)` → returns
  ```
  ### Sub-step
  foo
  ```
  (stops at `## Notes`, level 2 ≤ 3).
- `read_section(name, heading="Setup", level=4)` → no `####` heading → `null`.
- `read_section(name, heading="au")` against `## Auth` → `null` (exact text
  match only, not substring).

Default `level=2` because filename = title and convention reserves H1 for
nothing inside the body. `level` accepts 1–6 for flexibility (lets you
match a heading at a specific tier when the same text appears at multiple
tiers).

#### Heading parser (pinned, ATX only)

- Recognize **ATX headings only**: a line is a heading iff, after optional
  leading whitespace (0..3 spaces, CommonMark cap), it starts with a run
  of 1–6 `#`, followed by ≥ 1 space, then text.
- Heading **text** = everything after the `#`-run and the first space,
  with a trailing run of `#` (close-form `## foo ##`) stripped if present,
  then surrounding whitespace trimmed. `## foo ##` → text `foo`.
- **Setext headings (`Foo\n===` / `Foo\n---`) are NOT recognized** — the
  underline line is treated as body. Pinned to keep parser trivial.
- **Headings inside fenced code blocks** (``` or ~~~ of equal length) are
  **ignored**. The parser must track open fences and skip lines until the
  matching close fence before resuming heading detection. Indented code
  blocks (4-space) are not special-cased — they are rare in bodies and
  the cost of full CommonMark is unjustified for a note store.
- A blank line before the heading is not required (lenient), matching
  agent-authored bodies where blank-line discipline varies.

### Line-range semantics (`read_lines`)

- 1-based, inclusive on both ends. `start=10, end=20` returns lines 10–20.
- Validation: `start ≥ 1`, `end ≥ start`. Else `invalid_range`.
- Out-of-range `end` (beyond EOF) → clamp silently; returned `end` reflects
  actual last line read. `total_lines` always reports true file length.
- Empty file + `start=1, end=5` → `lines: ""`, `total_lines: 0`.
- Newlines preserved (lines joined with `\n`; trailing newline of last line
  kept as-is).
- Useful for paging through long bodies, or extracting an arbitrary slice
  the agent identified via `ls` (note mode) heading line numbers.

### mtime as ordering key

`recent` orders by file mtime desc. **Tiebreaker on equal mtime: `path`
ascending (byte order)** — deterministic across invocations. Reliable
when writes go through this server (server uses `os.replace` which
preserves mtime on the new file; body updates bump mtime via the write).
External tooling (rsync, tar, `cp -p` quirks) can skew mtimes —
documented limitation; not adjusted server-side.

---

## Error responses

All tools return structured errors with `code` (string snake_case) + `message`:

| Code              | When                                                         |
|-------------------|--------------------------------------------------------------|
| `already_exists`  | `create` / `rename` target file present                     |
| `not_found`       | `update_body` / `read` / `read_section` / `read_lines` / `delete` / `rename` source missing, or `ls` note-mode target missing |
| `dir_missing`     | `postit.create` on a dir that has no `TOPIC.md` yet (call `topic.create`); `topic.create` on a nested `dir` whose parent is not yet a topic (create the parent topic first). **Root is exempt** — root never carries a `TOPIC.md` and `postit.create` at root always succeeds. |
| `dir_exists`      | `topic.create` on an existing dir                           |
| `reserved_name`   | `name` is `TOPIC`                                            |
| `invalid_name`    | `name` fails validation (empty, `/`, `\0`, newline, leading `.`) |
| `invalid_path`    | `dir` fails normalization (`..`, absolute, `\0`)            |
| `invalid_range`   | `read_lines` with `start < 1` or `end < start`                |
| `too_large`       | resulting body > 1 MiB                                       |
| `no_op`           | `rename` where `new_name == name`                            |

These codes let the agent branch programmatically (e.g. on `dir_missing`,
retry with prior `topic.create`).

---

## Atomic writes + safety

- All body writes go through tmp-file **in same dir as target** + `os.replace`
  → atomic at FS level. Never partial content on crash mid-write. Tmp files
  cleaned up on failure.
- `rename` uses `os.replace` (atomic same-filesystem rename; same dir → always
  same FS).
- `topic.create` and `topic.write` write atomically too.
- **`postit.update_body` append is not crash-safe across the append step.**
  `overwrite` mode is atomic (single tmp + replace). `append` mode is
  read-modify-write: parses existing body, appends `content`, writes the
  whole result atomically. A crash **between the read and the replace**
  loses the appended content (the pre-existing body is safe — it was never
  mutated until the atomic replace). Acceptable for v1; no fix unless asked.
- Single-process stdio assumed → no `flock`. Multi-process pod (later) noted
  as a known limitation in Open Qs.

---

## Project layout

```
agent-postit/
├── README.md             (this file)
├── pyproject.toml        deps: mcp (FastMCP), no DB deps
├── uv.lock
├── Dockerfile            python:3.12-slim, uv install, entrypoint server
├── Containerfile         symlink → Dockerfile (podman convention)
├── compose.yaml          local stdio example (reference only)
├── src/
│   └── agent_postit/
│       ├── __init__.py
│       ├── __main__.py   `python -m agent_postit` entrypoint, argparse for `--root`
│       ├── server.py     FastMCP app + tool registration
│       ├── paths.py      normalization, validation, escaping protection
│       ├── store.py       fs ops: read/write/ls/walk/atomic-rename/delete
│       ├── search.py      regex walker for `postit.search`
│       ├── sections.py    markdown heading section extraction
│       └── models.py      Pydantic schemas for tool I/O
└── tests/
    ├── test_paths.py
    ├── test_store.py
    ├── test_search.py
    └── test_sections.py
```

---

## Containerfile sketch (planning only)

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    POSTIT_ROOT=/data
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev
VOLUME ["/data"]
ENTRYPOINT ["uv", "run", "python", "-m", "agent_postit"]
```

Local run (stdio transport):

```sh
podman run -i --rm \
  -v ~/.agent-postit:/data:Z \
  agent-postit:dev
```

`-i` keeps stdin open for stdio; no `-t` (would confuse line buffering).

---

## Auth — explicit non-decision

No auth, no rate limit in v1.

- Transport is stdio → trust boundary = "anyone who can spawn the process".
- Lock the box, not the protocol.
- If we ever switch to `http` transport (e.g. shared podman pod across
  hosts), **stop and reconsider**: bearer-token middleware becomes
  mandatory, `POSTIT_ROOT` becomes a network-attacked surface, file-write
  paths need audit. Do not silently enable http.

---

## Out of scope (v1)

- **Pinning** (sticky notes). Recall via `recent` / `ls` / `search` instead.
  Add a marker or frontmatter scheme later if a real need surfaces.
- **`topic.delete`.** Removing an empty topic dir is a manual `rmdir` (after
  deleting its last postit by hand; `postit.delete` keeps the dir per the
  locked decision). Add a guarded `topic.delete` (only on dir with no
  postits, refuses if `TOPIC.md` has content?) in v2. v1: shell out.
- Embeddings / semantic search. Revisit when note count > ~1000.
- Cross-directory move (only same-dir `rename` supported).
- Multi-process / remote transport / file locking.
- Web UI / TUI viewer (read files directly — they're plain `.md`).
- Cloud sync / backup (files are files; use rsync/git/whatever).
- TTL / expiry (no metadata store; would need frontmatter — skip for v1).
- Source attribution column (no schema; agent can include authorship lines
  in body if it cares).
- Total-tree storage cap. Per-note 1 MiB cap is enforced; total cap deferred.

---

## Open questions (resolved v1)

1. **Multi-process pod safety** — **defer.** stdio v1 stays single-process.
  Already documented as out of scope (multi-process / remote transport /
  file locking). If a real need arrives, migrate to `flock`+retry or back to
  SQLite; do not patch into v1.
2. **TOPIC.md format** — **plain body only, no frontmatter in v1.** Avoids
  adding a YAML parser and a structured-field surface. Revisit if/when a
  real metadata need (created_at, owner) appears.
3. **Search `line_no` column** — **keep.** 1-based index of the matched
  line in the body; useful for the agent Paging into a specific region via
  `read_lines` afterward.
4. **`ls` sorting** — **overridden to alphabetical mixing** (dirs and
  postits interleaved), truer to `ls -la`. Updated in the `postit.ls`
  row above. Recursive sort key = full relative path, byte order.
5. **`recent` body preview** — **no preview.** Keep `recent` cheap;
  caller fetches bodies selectively via `read` / `read_section`.

No open questions remain for v1.

---

## Local dev quickstart (when implemented)

```sh
uv sync
python -m agent_postit                  # uses ~/.agent-postit
python -m agent_postit --root ./notes   # override root
POSTIT_ROOT=/var/lib/agent-postit python -m agent_postit   # env override
```

CLI flag `--root <path>` overrides default `~/.agent-postit`. Env var
`POSTIT_ROOT` does the same (lower precedence than `--root`).

MCP client config (e.g. Zed) points stdio command at `python -m agent_postit`.

---

## Naming recap

- Project / repo dir: **`agent-postit`**
- Python package: `agent_postit`
- MCP server name (advertised to client): `agent-postit`
- Entrypoint: `python -m agent_postit`
- Default data dir: `~/.agent-postit` (env `POSTIT_ROOT` overrides)
- Note file extension: `.md`
- Topic marker file: `TOPIC.md` (reserved name)

---

## Bootstrap prompt for next session (implementation)

Paste this verbatim as the first user message in the next session:

> Implement `agent-postit` from the spec in `README.md`. The repo currently
> contains only `README.md` — no `src/`, no `pyproject.toml`, no
> `Dockerfile`. All design decisions are locked; **do not re-resolve** the
> items under "Open questions (resolved v1)" or "Discrete open items
> (resolved v1)" — both are audit-trail only.
>
> Your job, in order:
>
> 1. Read `README.md` end to end.
> 2. Follow the **Implementation order** section at the bottom of this file.
>    Build modules bottom-up: `paths.py` → `sections.py` → `store.py` →
>    `search.py` → `models.py` → `server.py` → `__main__.py` → packaging →
>    container. Skip nothing.
> 3. Use the **pyproject.toml sketch** and **Tool I/O model shapes**
>    subsections (bottom of this file) as direct references — they are part
>    of the spec, not suggestions.
> 4. Write tests alongside each module per the **Smoke / acceptance tests**
>    subsection. Run `uv run pytest` after each module lands. Do not move on
>    until green.
> 5. If you hit a genuine contradiction or hole in the spec, **stop and
>    report** with line refs; do not paper over with ad-hoc choices.
> 6. Do not edit the locked **Decisions (locked)** table or any *resolved*
>    section. If a spec change is truly needed, surface it explicitly.
>
> Stay in caveman mode for prose. Code, schema, commits, and this prompt
> stay normal English.

---

## pyproject.toml sketch

Reference for `pyproject.toml`. Match exactly except for version bump.

```toml
[project]
name = "agent-postit"
version = "0.1.0"
description = "Local stdio MCP server: agent post-it memory on the filesystem"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
dependencies = [
  "mcp>=1.2",
]

[project.scripts]
agent-postit = "agent_postit.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agent_postit"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.23",
]
```

Notes:
- The `[project.scripts]` shim is **optional** — `python -m agent_postit`
  remains the canonical entrypoint and is what MCP clients should invoke
  (`-m` keeps the stdio transport path simple, no extra process layer).
  The script shim is for convenience when running outside an MCP client.
- `mcp` pulls FastMCP (the `mcp` PyPI package exposes `FastMCP`).
- No DB deps. No `pydantic` pin — FastMCP brings its own pydantic for tool
  schema generation; do not add a top-level `pydantic` dep unless a model
  module outside the tool-layer needs it.
- `hatchling` chosen because uv default works cleanly with src-layout.

---

## Tool I/O model shapes

Pydantic-ish shapes for each tool's input and return. Use these as the
FastMCP tool argument types and return dataclasses — straight from spec
rows above, restated here so `server.py` registration is mechanical.

### Topic verbs

```python
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
```

### Postit CRUD

```python
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
```

### High-level

```python
class PostitLsIn(BaseModel):
    dir: str | None = None
    name: str | None = None
    recursive: bool = False

class LsDirItem(BaseModel):
    type: Literal["dir"] = "dir"
    name: str
    has_topic: bool
    topic_preview: str | None = None  # first ~80 chars of TOPIC.md, None if no topic

class LsPostitItem(BaseModel):
    type: Literal["postit"] = "postit"
    name: str
    mtime: float
    size: int

class LsNoteModeOut(BaseModel):
    name: str
    dir: str
    total_lines: int
    headings: list[Heading]

class Heading(BaseModel):
    level: int      # 1..6
    heading: str    # parsed text (close-form # stripped, trimmed)
    line_no: int    # 1-based

class PostitSearchIn(BaseModel):
    pattern: str                 # Python re regex
    scope: Literal["name", "body", "both"] = "both"
    dir: str | None = None
    recursive: bool = True
    limit: int = 50

class BodyMatch(BaseModel):
    line_no: int
    line: str

class SearchHit(BaseModel):
    path: str           # full relative path (dir + "/" + name), "" for root
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
```

### Error shape

All tool errors return a structured object, not a bare string:

```python
class ToolError(BaseModel):
    code: str   # snake_case, from the error table
    message: str
```

FastMCP raising: prefer returning `ToolError` from the tool fn and letting
the agent branch on `code`. FastMCP's own exception path mangling the
`code` field is undesirable. (If FastMCP mandates raise-and-let-it-serialize,
switch at implementation time — flagged here so it's not a surprise.)

---

## Smoke / acceptance tests

Minimum test set. Each module's tests land alongside the module (per the
implementation order). `uv run pytest` must be green before moving on.

### `tests/test_paths.py`
- normalize(`""`), `"."`, `"/"` all → `""` (root sentinel)
- normalize(`"foo/"`), `"/foo"` → `"foo"`
- normalize rejects `..`, absolute, `\0`, component-with-`/`
- valid names: `"foo"`, `"Foo Bar"`, `"a.b"` accepted
- invalid names: `""`, `"TOPIC"`, `"a/b"`, `"\\0"`, `"\\n"`, `".hidden"`, `"."` → `invalid_name`

### `tests/test_sections.py`
- `## Setup\n do X\n ### Sub-step\n foo\n ## Notes\n bar`:
  - read `Setup` (default level=2) → returns `## Setup\n do X\n ### Sub-step\n foo\n`
  - read `setup`, `SETUP` → same result (case-insensitive)
  - read `Sub-step`, level=3 → `### Sub-step\n foo\n`
  - read `Setup`, level=4 → `None`
  - read `au` against `## Auth` → `None` (exact match, not substring)
- fenced block: a `## Heading` inside ` ``` ` fence → not parsed as heading
- setext `Foo\n===` → not a heading; `===` is body
- close-form `## foo ##` → text `foo`
- leading 0..3 spaces before `#` accepted; 4 spaces → body (indented code, ignored as heading)

### `tests/test_store.py`
- `topic.create("t1", "desc")` → dir + `TOPIC.md` exist
- `topic.create("t1", ...)` again → `dir_exists`
- `topic.create("a/b", ...)` before `a` is a topic → `dir_missing`
- `topic.create("a", ...)`, then `topic.create("a/b", ...)` → ok
- `postit.create("note", "body", dir="t1")` → file exists with body
- `postit.create(..., dir="nope")` → `dir_missing`
- `postit.create` existing name → `already_exists`
- `postit.create("TOPIC", ...)` → `reserved_name`
- `postit.create` with body > 1 MiB → `too_large`
- `postit.update_body` overwrite replaces; append adds with trailing-newline fixup
- `postit.update_body` on missing note → `not_found`
- `postit.rename` same name → `no_op`; to existing → `already_exists`; to `TOPIC` → `reserved_name`; otherwise ok and old name gone
- `postit.delete` removes file, keeps dir; re-delete → `not_found`
- `postit.read` returns body + mtime + size; missing → `not_found`
- `postit.read_lines` valid range; beyond-EOF clamps; `start<1` / `end<start` → `invalid_range`; empty file → `lines=""`, `total_lines=0`
- After deleting the last postit in a topic dir, dir + `TOPIC.md` still exist

### `tests/test_search.py`
- glob of notes; `search("foo")` returns hits with `body_matches` line_no + full line
- `scope="name"` skips body; `scope="body"` skips name
- `TOPIC.md` never matched
- case-insensitive default; `(?-i)` honored
- `limit` caps results

### `tests/test_server.py` (light)
- spawn the FastMCP app in-process (or via stdio) and call each tool with
  stub args; assert return shape matches the I/O model. No live filesystem —
  use a tmp `POSTIT_ROOT` via `tmp_path`.
- assert every tool name advertised matches the spec (`topic.create`,
  `postit.create`, etc.) — catches typos in registration.

---

## Implementation order

Build bottom-up. Land tests with each module. Do not skip ahead.

1. **`pyproject.toml`** — copy the sketch above. `uv sync` to install `mcp`
   and dev deps. Confirm `python -c "import agent_postit"` works (after
   step 2 creates the package).
2. **`src/agent_postit/__init__.py`** — empty or a single `__version__ =
   "0.1.0"` line.
3. **`src/agent_postit/paths.py`** — `normalize_dir`, `validate_name`.
   Land `tests/test_paths.py`. Green.
4. **`src/agent_postit/sections.py`** — heading parser + section extractor.
   Land `tests/test_sections.py`. Green.
5. **`src/agent_postit/store.py`** — fs ops: `topic_create`, `topic_read`,
   `topic_write`, `postit_create`, `postit_update_body`, `postit_rename`,
   `postit_delete`, `postit_read`, `postit_read_section`, `postit_read_lines`,
   `postit_ls`. All atomic-write paths here. Land `tests/test_store.py`.
   Green.
6. **`src/agent_postit/search.py`** — regex walker. Land
   `tests/test_search.py`. Green.
7. **`src/agent_postit/models.py`** — Pydantic schemas per the I/O shapes
   above. Importable; no tests needed beyond import.
8. **`src/agent_postit/server.py`** — FastMCP app, register every tool,
   delegate to `store` / `search`. Land `tests/test_server.py` (light).
   Green.
9. **`src/agent_postit/__main__.py`** — `argparse` for `--root`, resolve
   root via precedence (`--root` > `POSTIT_ROOT` > `~/.agent-postit`),
   ensure root dir exists (`mkdir -p`), hand off to `server.run()`.
   Smoke: `python -m agent_postit --help` exits 0; `python -m agent_postit`
   with a MCP client round-trips one tool call.
10. **`Dockerfile`** — copy the Containerfile sketch. `podman build -t
    agent-postit:dev .`. `Containerfile` symlink → `Dockerfile`. `compose.yaml`
    minimal stdio example (reference only).
11. **Re-read `README.md`**, confirm modules match the spec's locked
    decisions. Fix drift, not spec.

---

## Discrete open items (resolved v1)

All eight carried-over items decided. Resolutions folded into the spec
proper; this section kept as an audit trail.

1. **Setext headings** → ATX only. Pinned in the **Heading parser**
   subsection under **Section semantics**; setext underline lines treated as
   body.
2. **No `topic.delete`** → deferred to v2. Manual `rmdir` in v1. Recorded in
   **Out of scope (v1)**.
3. **`update_body` append not crash-safe** → accepted, documented in
   **Atomic writes + safety**. No fix unless asked.
4. **Case-sensitive FS assumed** → documented as a caveat under **Path
   normalization** (macOS / case-insensitive mounts).
5. **`recent` tiebreaker on mtime tie** → `path` ascending (byte order).
   Documented in **mtime as ordering key**.
6. **Env var precedence** → `--root` > `POSTIT_ROOT` > default
   `~/.agent-postit`, no other rules. Already documented in **Local dev
   quickstart**.
7. **`topic.create` with empty `description`** → allowed; produces a
   zero-length `TOPIC.md`. Confirmed in the `topic.create` row.
8. **Heading parser details** → pinned in the **Heading parser** subsection
   (optional 0..3 leading spaces, 1–6 `#`, ≥1 space, close-form `#` preserved
   and stripped from text, fenced code blocks excluded).