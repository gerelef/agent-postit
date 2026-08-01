# agent-postit

A local MCP (Model Context Protocol) server that gives an agent persistent
"post-it" memory — small notes, tidbits, and reminders — stored on the
**native filesystem** as plain `.md` files. Reach it over **stdio** with
**no auth**: any local process that can spawn the server can read, write,
and delete notes.

The mental model is a human's corkboard. An agent scribbles a sticky note
when it learns something it wants to recall later, files it under a topic,
and glances back at the board at the start of the next session.

---

## What it is

_agent-postit_ is an MCP server a software agent can call as one of its
tools. The server exposes a small set of verbs for creating and recalling
short notes. Notes are plain Markdown files on disk; nothing is hidden in
a database, so the notes are also readable, greppable, and editable by
hand. There is no authentication, no rate limiting, no multi-user model —
the trust boundary is "whoever can spawn the process can do anything".

It is a **local** tool: it speaks the stdio transport and writes to a
directory you own. There is no network surface.

---

## Concepts

- **Notes** are `.md` files. The filename (without `.md`) **is** the note's
  name — no slugification, but **all names are case-folded to lowercase** on
  the way in (see [Case folding](#note-name-and-path-case-folding)). The body
  is whatever Markdown the agent writes.
- **Topics** are directories that group related notes. Each topic dir
  contains a `TOPIC.md` file describing what the topic is about. Topics
  are created explicitly (see `topic.create`); they are never auto-created
  by writing a note into a missing dir.
- **Root** (`/`) is a topic-less catch-all for generic notes — things that
  don't belong to any one topic, or that the agent hasn't filed yet. Root
  has no `TOPIC.md` and is not itself a topic.
- Topics may nest (a topic inside a topic); each nested level gets its own
  `TOPIC.md` via its own `topic.create`.
- `TOPIC` is a reserved note name: you cannot create a note called
  `TOPIC.md` directly in any directory. The reserved-name check is
  case-insensitive, so `Topic`, `topic`, and `TOPIC` are all rejected.
- `TOPIC.md` files are never searched or listed as postits — they only
  appear through `topic.read` / `topic.write`.

---

## Tools

All tools take a single object argument (named `arg` over the wire) with
the fields listed below. Paths are addressed by `(dir, name)` — there are
no integer IDs. `dir` defaults to the root.

### Topic tools

`topic.create`

- Args: `dir` (required), `description` (required, may be `""`).
- Creates the directory at `dir`, then writes `TOPIC.md` with `description`
  as its body. Atomic on write.
- Refuses if `dir` already exists → `dir_exists`. No adoption of stray
  directories: onboard a stray dir by deleting it first or by moving its
  contents into a freshly-created topic.
- The parent of `dir` must already be a topic (or `dir` must live at
  root). Creating `/a/b` requires `/a` to already have a `TOPIC.md`;
  otherwise → `dir_missing` with a hint to create the parent first.
- This is the only way to make a new topic. Topics are built one level at
  a time, top-down.

`topic.read`

- Args: `dir` (required).
- Returns the `TOPIC.md` body for that directory, or `null` if the
  directory or its `TOPIC.md` is missing.

`topic.write`

- Args: `dir` (required), `description` (required).
- Overwrites `TOPIC.md` with `description`.
- Refuses if the directory is missing → the caller must `topic.create`
  first.

### Postit CRUD

`postit.create`

- Args: `name` (required), `body` (required, may be `""`), `dir?`
  (defaults to root).
- Writes `<dir>/<name>.md` atomically (tmp file + rename).
- Refuses if the dir is missing → `dir_missing` with a hint to
  `topic.create` first.
- Refuses if the file already exists → `already_exists`.
- An empty `body` is allowed and produces a zero-length `.md` file.

`postit.update_body`

- Args: `name` (required), `content` (required), `mode` (`"append"` |
  `"overwrite"`, default `"overwrite"`), `dir?`.
- `overwrite` writes `content` atomically.
- `append` reads the existing body, concatenates `content` (inserting a
  trailing newline if the existing body lacks one), writes back atomically.
- Refuses if the note is missing → `not_found`.
- The 1 MiB size cap applies to the resulting body.

`postit.rename`

- Args: `name` (required), `new_name` (required), `dir?`.
- Renames `<name>.md` to `<new_name>.md` within the same directory.
- Refuses if `new_name == name` → `no_op`, or if `<new_name>.md` already
  exists → `already_exists`. Same-directory only; cross-dir move is out
  of scope. `new_name` runs through note-name validation (and is
  lowercased), so `OldName` → `NEWNAME` resolves to `oldname` → `newname`.

`postit.delete`

- Args: `name` (required), `dir?`.
- Removes `<dir>/<name>.md`. The directory is left in place even if now
  empty — empty topics survive. Re-deleting a missing note → `not_found`.

`postit.read`

- Args: `name` (required), `dir?`.
- Returns `{name, dir, body, mtime, size}`. `not_found` if missing.
  For large bodies you may prefer `read_section` or `read_lines`.

`postit.read_section`

- Args: `name` (required), `heading` (required, case-insensitive exact
  text match), `level?` (1–6, default `2`), `dir?`.
- Returns the body text starting at the first heading whose text equals
  `heading` at the given `level`, and **everything under it until the
  next heading of level ≤ `level` or EOF** — i.e. the matched heading
  line plus all subheaders and their content, verbatim. Returns `null`
  if no heading matches.
- Match is exact text, not substring. `read_section("auth")` matches a
  heading whose text is `Auth` but not `Authorization`.

`postit.read_lines`

- Args: `name` (required), `start` (1-based, required), `end` (1-based
  inclusive, required), `dir?`.
- Returns `{name, dir, start, end, total_lines, lines}` — the joined
  text of body lines `start..end` (inclusive, 1-based).
- Validation: `start ≥ 1`, `end ≥ start`, else `invalid_range`. An `end`
  beyond EOF is silently clamped; the returned `end` reflects the actual
  last line read. `total_lines` always reports the true file length.
- Newlines are preserved. An empty file with `start=1, end=5` returns
  `lines: ""`, `total_lines: 0`.

### High-level tools

`postit.ls`

- Args: `dir?` (defaults to root), `name?`, `recursive?` (default
  `false`, only used in dir mode).
- **Dir mode** (`name` absent): returns a single flat `ls -la`-style
  list of items in `dir`, with directories and postits interleaved
  alphabetically (no dirs-first block). Each dir item reports whether it
  has a `TOPIC.md` and a short preview of its description. Each postit
  item reports its `mtime` and `size`. Sort is byte-order on name. With
  `recursive=true`, the entire subtree under `dir` is walked into one
  flat list with full relative paths in `name`; the sort key becomes the
  full relative path.
- **Note mode** (`name` set): returns `{name, dir, total_lines, headings}`
  — a list of the Markdown headings in that file (level, text, 1-based
  line number) in document order, with **no body content**. Useful as a
  table of contents before deciding what to `read_section` or
  `read_lines`. `recursive` is ignored in note mode.
- `TOPIC.md` is never listed. Foreign files (non-`.md`) are ignored.
  `name="TOPIC"` → `reserved_name`. `not_found` if the note is missing.

`postit.search`

- Args: `pattern` (Python `re` regex), `scope` (`"name" | "body" |
  "both"`, default `"both"`), `dir?` (defaults to root), `recursive?`
  (default `true`), `limit?` (default 50).
- Walks the subtree, applies `re.search` across names and/or bodies
  (case-insensitive by default; embed `(?-i)` in the pattern to make it
  case-sensitive). Returns matches: one entry per hit with full lines
  containing the match (grep-like), the matching line numbers, and a flag
  for whether the name itself matched.
- Empty-body notes match on name when `scope` includes `name`. Caps at
  `limit`. Skips `TOPIC.md`.

`postit.recent`

- Args: `limit?` (default 10), `dir?` (defaults to root).
- Walks the **subtree** rooted at `dir` (always recursive — there is no
  opt-out; nested-topic notes surface when you point it at a parent).
  Sorts postits by `mtime` descending with `path` ascending as the
  tiebreaker, and returns the top `limit` as `{path, name, mtime, size}`
  with no body. Default `dir=root` returns every postit across the whole
  tree.
- Use this at session start to reload context — the equivalent of
  glancing at the corkboard. Body content is deliberately not included;
  call `postit.read` / `read_section` / `read_lines` to fetch what you
  want to keep the call cheap.

### Note-name validation

`name` arguments are validated server-side:

- Reject if it contains `/`, a NUL byte, or a newline.
- Reject if it begins with `.` (dotfile confusion).
- Reject if it equals `TOPIC` (reserved — case-insensitive: `TOPIC`, `Topic`,
  `topic` are all rejected because the name is lowercased before the check).
- Reject if empty.
- Otherwise the name is **lowercased**, used with `.md` appended. The note's
  on-disk filename is always lowercase.

### Note-name and path case folding

All `dir` and `name` arguments are **case-folded to lowercase** on the way
in. This applies to every operation — create, read, update, rename, delete,
`ls`, `search`, and `recent`. Two consequences worth knowing:

- **Names you pass are not preserved on disk.** A note created with the name
  `Recall` lands on disk as `recall.md`. Creating `Foo` and then trying to
  create `foo` is an `already_exists` error: after folding they are the same
  name. Reading, listing, searching, and deleting accept any case and all
  resolve to the same note.
- **Reserved-name matching is case-insensitive.** The reserved basename is
  `TOPIC` (i.e. on-disk `topic.md`); any case variant the caller passes is
  rejected.

Topic dirs work the same way: `topic.create` and every other verb fold the
`dir` argument's components to lowercase. Creating the topic `MyTopic` lands
on disk as `mytopic/`.

#### Foreign files on disk

The server only ever writes lowercase `.md` filenames and lowercase topic
dir names. If you hand-place a file on disk that does not match the expected
shape, the rule is:

- A file whose **suffix is not exactly lowercase `.md`** (e.g. `Foo.MD`,
  `Foo.Md`) is **foreign**: it is never listed by `postit.ls`, never
  searched by `postit.search`, never returned by `postit.recent`. It is
  invisible to the agent.
- A file whose suffix **is** lowercase `.md` but whose lowercased basename
  equals `topic` (e.g. a stray `Topic.md` you dropped in by hand) is treated
  as the topic marker for that directory and is likewise never surfaced as
  a postit.

So a hand-created `Foo.MD` coexisting with a server-created `foo.md` is
fine — only `foo.md` is visible, and `Foo.MD` is silently ignored.

### Encoding and size

- All files are UTF-8 with no BOM. CRLF is not normalized.
- A 1 MiB cap applies per `postit.create` and `postit.update_body` call,
  measured against the resulting body. Larger is refused with `too_large`.

### Errors

Errors are returned, not raised, as `{code, message}` objects. Codes:

- `dir_exists` — `topic.create` target dir already exists.
- `dir_missing` — parent dir not a topic, or target dir not present.
- `already_exists` — a note with that name already exists in that dir.
- `not_found` — note (or topic) not found.
- `no_op` — rename target equals source.
- `reserved_name` — `name` is `TOPIC` (or otherwise reserved).
- `invalid_name` — `name` failed validation.
- `invalid_path` — `dir` failed normalization.
- `invalid_range` — `read_lines` bounds wrong.
- `too_large` — body write exceeds 1 MiB.

---

## Running locally

The server is a single Python entrypoint, `python -m agent_postit`. You can
run it directly with `uv` for development, or as a container for a clean,
isolated install.

### Configure the data root

The data root is resolved in this order (highest precedence first):

1. the `--root <path>` CLI flag,
2. the `POSTIT_ROOT` environment variable,
3. the default `~/.agent-postit`.

The root directory is created (`mkdir -p`) at startup if missing.

### Run with `uv` (fast, no container)

From a checkout of this repo:

```sh
uv sync                       # install deps (incl. dev) into ./.venv
uv run python -m agent_postit # serves stdio MCP at default root
```

Override the root:

```sh
uv run python -m agent_postit --root ./my-notes
# or
POSTIT_ROOT=/var/lib/agent-postit uv run python -m agent_postit
```

A `help` invocation prints the same precedence:

```sh
uv run python -m agent_postit --help
```

The optional console shim `agent-postit` (declared in `pyproject.toml`)
is also installed by `uv sync`; `uv run agent-postit` works identically to
`uv run python -m agent_postit`.

### Connect an MCP client

Point your MCP client at the running process' stdio. The exact config
depends on the client; for **Zed** see the next subsection. Other clients
(Claude Desktop, custom harnesses) follow the same pattern: spawn
`python -m agent_postit` as a child process and speak JSON-RPC over its
stdin/stdout.

#### Configure in Zed

Zed reads MCP server config from its settings file (open with
`zed: open settings file`, or edit from **Settings → AI → MCP Servers**).
Local stdio servers live under `context_servers` with a `command`, `args`,
and optional `env`. No shell expansion is performed inside `command` /
`args` — use absolute paths and literal strings.

**Option A — `uv` (recommended).** Faster cold-start, no per-session
container, logs straight to a shell if you run the same command manually.
Assumes the repo is checked out at `/home/leviticus/random/remember-me`:

```jsonc
{
  "context_servers": {
    "agent-postit": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/home/leviticus/random/remember-me",
        "python",
        "-m",
        "agent_postit",
        "--root",
        "/home/leviticus/.agent-postit"
      ],
      "env": {}
    }
  }
}
```

**Option B — container.** Same protocol, podman drives the image built
above. Notes land in the host directory you bind-mount at `/data`.

```jsonc
{
  "context_servers": {
    "agent-postit": {
      "command": "podman",
      "args": [
        "run",
        "-i",
        "--rm",
        "-v",
        "/home/leviticus/.agent-postit:/data:Z",
        "agent-postit:dev"
      ],
      "env": {}
    }
  }
}
```

Drop `:Z` on non-SELinux hosts and substitute `docker` for `podman` if
that's your runtime. Don't add `--tty` / `-t` — it breaks line buffering
for stdio protocols.

**Verify the server is live.** In Zed open **Settings → AI → MCP
Servers** and watch the indicator dot next to `agent-postit`. Green with
the tooltip "Server is active" means the handshake succeeded and the
13 tools have been registered. Red indicates an error; hover for details
(typically an absolute-path typo or a missing `--root`).

The tools surface to the agent as `mcp:agent-postit:<tool>`, e.g.
`mcp:agent-postit:postit.recent` at session start.

#### Tool permissions (Zed)

By default Zed prompts before every tool call. For dogfooding it's sane
to auto-allow the no-side-effect reads, keep creates/writes on `confirm`
so you can sanity-check what's about to land, and always prompt for
delete (no trash bin, no undo in v1).

Per-tool entries use the `mcp:<server>:<tool>` key and override the
global `agent.tool_permissions.default`. Anything not listed inherits
that default.

```jsonc
{
  "agent": {
    "tool_permissions": {
      "default": "confirm",
      "tools": {
        // Read-only: no state change → auto-allow.
        "mcp:agent-postit:topic.read":          { "default": "allow" },
        "mcp:agent-postit:postit.read":         { "default": "allow" },
        "mcp:agent-postit:postit.read_section": { "default": "allow" },
        "mcp:agent-postit:postit.read_lines":   { "default": "allow" },
        "mcp:agent-postit:postit.ls":           { "default": "allow" },
        "mcp:agent-postit:postit.search":       { "default": "allow" },
        "mcp:agent-postit:postit.recent":       { "default": "allow" },

        // Create / modify: confirm so you see what's being written.
        // (Left at the global default; flip individual ones to "allow"
        // once you trust them, e.g. postit.create after a few sessions.)
        "mcp:agent-postit:topic.create":        { "default": "confirm" },
        "mcp:agent-postit:topic.write":         { "default": "confirm" },
        "mcp:agent-postit:postit.create":       { "default": "confirm" },
        "mcp:agent-postit:postit.update_body":  { "default": "confirm" },
        "mcp:agent-postit:postit.rename":       { "default": "confirm" },

        // Delete: irreversible, no trash, no v1 undo → keep on confirm.
        "mcp:agent-postit:postit.delete":        { "default": "confirm" }
      }
    }
  }
}
```

Suggested progression: start with the block above as-is. After a session
or two `postit.create` and `postit.update_body` get noisy — flip those
two to `allow` and leave `delete` permanently on `confirm`. The cost of
an errant append is an `update_body` overwrite; the cost of an errant
delete is gone forever.

For full reference see the [Zed MCP guide](https://zed.dev/docs/ai/mcp)
and the [tool permissions doc](https://zed.dev/docs/agent/tool-permissions).

### Run as a container (podman or docker)

The repo ships a prod-ready multi-stage `Dockerfile`. `Containerfile` is a
symlink to it (podman convention). The runtime stage runs as a non-root
user (`agentpostit`, uid/gid 1001), carries only the isolated venv (no
`uv`, no `pip`, no source tree), and writes notes to `/data` (matched by
the `POSTIT_ROOT=/data` env inside the image).

Build:

```sh
podman build -t agent-postit:dev .
# or, equivalently:
podman build -f Containerfile -t agent-postit:dev .

# docker works the same:
docker build -t agent-postit:dev .
```

Run (stdio transport — keep stdin open, do **not** add `-t`):

```sh
podman run -i --rm \
  -v ~/.agent-postit:/data:Z \
  agent-postit:dev

# docker works the same:
docker run -i --rm \
  -v ~/.agent-postit:/data \
  agent-postit:dev
```

`-i` keeps stdin open for the JSON-RPC stream. `-t` breaks line buffering
for stdio protocols, so leave it off. The `:Z` suffix on podman relabels
the bind mount for SELinux (drop it on non-SELinux hosts).

Notes on the container:

- Files written inside the container land as container uid 1001.
  On the host this maps either into podman's subuid range (default
  behavior) or to your own uid if you pass `--user $(id -u):$(id -g)` to
  override. Both are valid; pick whichever matches how you want the
  host-side files owned.
- Final image size is ~160 MB (`python:3.12-slim-bookworm` base plus the
  `mcp` dependency tree).
- No `HEALTHCHECK` is defined: a stdio server has no network surface to
  probe.

---

## Name recap

- Project name: **agent-postit**.
- Package import name: `agent_postit`.
- MCP server name (advertised to clients): `agent-postit`.
- Canonical entrypoint: `python -m agent_postit`.

---

## License

MIT.