# agent-postit

_agent-postit_ is a small local server that gives an AI agent a place to
scribble notes — a memory, so things it learns in one session are still
around in the next.

Think of it like a corkboard with sticky notes. The agent writes a note
when it picks up something it wants to recall later (a fact, a decision,
a checklist, a bug it's chasing), files it under a topic, and glances at
the board at the start of the next session to remember where it left off.

It speaks [MCP](https://modelcontextprotocol.io) over HTTP on
`127.0.0.1:8000`, so any MCP-aware editor or agent (Zed, a CLI harness,
another agent) can point at it. Notes are plain Markdown files on your
disk under `~/.agent-postit/` — nothing is hidden in a database, so you
can read, grep, and edit them by hand too.

There is **no auth** and **no network exposure**: the server binds the
loopback interface. Whoever can reach `127.0.0.1` on your machine can
read and write notes; nobody else can. If you ever want remote reach, put
a TLS-terminating reverse proxy with auth in front and leave the server
behind it on loopback.

---

## Build, install, run

You need Python 3.12+ and [`uv`](https://github.com/astral-sh/uv).

```sh
git clone https://github.com/gerelef/agent-postit.git && cd agent-postit
uv sync                       # install deps (incl. dev) into ./.venv
uv run python -m agent_postit # serves MCP at http://127.0.0.1:8000/mcp
```

Liveness probe (no client needed):

```sh
curl -s http://127.0.0.1:8000/healthz   # → ok
```

Override the data root / host / port via flags or env:

```sh
uv run python -m agent_postit --root ./my-notes --port 8011
# or
POSTIT_ROOT=/var/lib/agent-postit POSTIT_PORT=8011 uv run python -m agent_postit
```

### Container (podman or docker)

The repo ships a prod-ready multi-stage `Dockerfile`. `Containerfile` is
a symlink to it (podman convention). Build:

```sh
podman build --format docker -t agent-postit:latest .
# docker uses the docker image format by default, so no flag is needed:
docker build -t agent-postit:latest .
```

`--format docker` makes podman emit the Docker image format so the
`HEALTHCHECK` instruction in the `Dockerfile` is honored (OCI format, the
podman default, has no `HEALTHCHECK` field and silently drops it).

Run (HTTP transport — bind loopback on the host side, no `-i`, no `-t`):

```sh
podman run --rm --name agent-postit \
  -p 127.0.0.1:8000:8000 \
  -v ~/.agent-postit:/data:Z \
  agent-postit:latest

# docker works the same (drop the :Z suffix):
docker run --rm --name agent-postit \
  -p 127.0.0.1:8000:8000 \
  -v ~/.agent-postit:/data \
  agent-postit:latest
```

The `127.0.0.1:` prefix on the publish flag is the loopback guarantee —
do not drop it unless you intend to expose the port to other hosts
(and have a reverse proxy with auth in front).

### Under `systemd --user` (rootless quadlet)

A reference rootless quadlet is shipped at
[`contrib/agent-postit.container`](contrib/agent-postit.container).
Quadlet (podman 4.4+) is podman's native systemd integration: drop a
`.container` file under `~/.config/containers/systemd/`, reload systemd,
and the podman generator emits a regular `agent-postit.service` from it
on boot — no hand-written `podman run` line in the unit.

Build the `agent-postit:latest` image, install the quadlet file, and start:

```sh
mkdir -p ~/.config/containers/systemd
cp contrib/agent-postit.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now agent-postit.service
journalctl --user -u agent-postit.service -f  # follows stdout/uvicorn
systemctl --user status agent-postit.service  # verify everything works
```

---

## Tools

The server advertises **13 tools** under the `agent-postit` MCP server
name. Tools surface to clients as `mcp:agent-postit:<tool>` (e.g.
`mcp:agent-postit:postit.recent`). All tools take a single object
argument named `arg` over the wire. Paths are addressed by `(dir, name)`
— there are no integer IDs. `dir` defaults to the root `/`.

Notes are `.md` files; the filename (minus `.md`) is the note's name.
Both `dir` and `name` are **case-folded to lowercase** on the way in —
creating a note called `Recall` lands on disk as `recall.md`, and
reading/listing/deleting it accepts any case. `TOPIC` is a reserved name
(case-insensitive: `Topic`, `topic`, `TOPIC` all rejected).

### Topic tools

- **`topic.create`** — `dir` (required), `description` (required, may be
  `""`). Creates the directory and writes `TOPIC.md` with the
  description. Refuses if `dir` already exists (`dir_exists`); refuses if
  the parent dir is not already a topic (`dir_missing`, with a hint to
  create the parent first). Topics are built top-down, one level at a
  time. This is the only way to make a new topic.
- **`topic.read`** — `dir` (required). Returns the `TOPIC.md` body for
  that directory, or `null` if missing.
- **`topic.write`** — `dir` (required), `description` (required).
  Overwrites `TOPIC.md`. Refuses if the directory is missing.

### Postit CRUD

- **`postit.create`** — `name` (required), `body` (required, may be
  `""`), `dir?`. Writes `<dir>/<name>.md` atomically. `dir_missing` if
  the dir is not a topic; `already_exists` if the file is there.
- **`postit.update_body`** — `name` (required), `content` (required),
  `mode` (`"append"` | `"overwrite"`, default `"overwrite"`), `dir?`.
  `overwrite` writes atomically; `append` reads the existing body,
  concatenates `content` (inserting a trailing newline if needed), and
  writes back atomically behind a per-note lock. `not_found` if missing.
  1 MiB cap on the resulting body (`too_large`).
- **`postit.rename`** — `name` (required), `new_name` (required), `dir?`.
  Renames within the same directory. `no_op` if `new_name == name`;
  `already_exists` if the target is there. Same-dir only.
- **`postit.delete`** — `name` (required), `dir?`. Removes the file. The
  directory is left in place even if now empty. `not_found` if missing.
- **`postit.read`** — `name` (required), `dir?`. Returns
  `{name, dir, body, mtime, size}`. `not_found` if missing. For large
  bodies prefer `read_section` or `read_lines`.
- **`postit.read_section`** — `name` (required), `heading` (required,
  case-insensitive exact text match), `level?` (1–6, default `2`),
  `dir?`. Returns the matched heading line plus everything under it
  until the next heading of level ≤ `level` or EOF — i.e. the section
  and all its subheaders, verbatim. `null` if no heading matches. Match
  is exact text, not substring: `read_section("auth")` matches `Auth`
  but not `Authorization`.
- **`postit.read_lines`** — `name` (required), `start` (1-based,
  required), `end` (1-based inclusive, required), `dir?`. Returns
  `{name, dir, start, end, total_lines, lines}` — body lines `start..end`
  inclusive. `invalid_range` if `start < 1` or `end < start`. `end`
  beyond EOF is silently clamped; the returned `end` reflects the actual
  last line.

### High-level tools

- **`postit.ls`** — `dir?` (defaults to root), `name?`, `recursive?`
  (default `false`, dir mode only).
    - **Dir mode** (`name` absent): a flat `ls -la`-style list of items in
      `dir`, dirs and postits interleaved alphabetically. Each dir item
      reports whether it has a `TOPIC.md` and a short preview of its
      description; each postit item reports `mtime` and `size`. With
      `recursive=true`, the whole subtree is walked into one flat list
      with full relative paths as the `name`, sort key = full relative
      path.
    - **Note mode** (`name` set): `{name, dir, total_lines, headings}` —
      the Markdown headings in that file (level, text, 1-based line
      number), document order, **no body content**. Useful as a table of
      contents before `read_section` / `read_lines`. `recursive` ignored.
    - `TOPIC.md` is never listed. Foreign files (non-`.md`) are ignored.
- **`postit.search`** — `pattern` (Python `re` regex), `scope`
  (`"name" | "body" | "both"`, default `"both"`), `dir?` (defaults to
  root), `recursive?` (default `true`), `limit?` (default 50). Walks
  the subtree, applies `re.search` case-insensitively (embed `(?-i)` to
  make it case-sensitive). Returns one entry per hit with full matching
  lines (grep-like), line numbers, and a flag for whether the name
  itself matched. Caps at `limit`. Skips `TOPIC.md`.
- **`postit.recent`** — `limit?` (default 10), `dir?` (defaults to root).
  Walks the subtree rooted at `dir` (always recursive — no opt-out),
  sorts by `mtime` descending with `path` ascending as tiebreaker,
  returns the top `limit` as `{path, name, mtime, size}` with no body.
  Default `dir=root` returns every postit across the whole tree. Use at
  session start to reload context — body is deliberately not included.

### 'Hello World' notes for an agent that doesn't know where to start

- At session start: call **`postit.recent`** with no args to see what
  you noted last. Optional: `postit.ls` the root or a project topic to
  see what topics exist.
- Want to peek at a note without pulling the whole body? `postit.ls`
  with `name` set gives you the table of contents; then `read_section`
  for the part you care about, or `read_lines` for an exact range.
- Lost a note? `postit.search` with a regex over names and bodies,
  recursive from root.
- Filing things: `topic.create` first (top-down), then `postit.create`
  into it. Root `/` is fine for things that don't belong anywhere yet.

### Errors

Errors are returned, not raised, as `{code, message}` objects. Codes:

- `dir_exists`, `dir_missing`, `already_exists`, `not_found`, `no_op`
- `reserved_name`, `invalid_name`, `invalid_path`, `invalid_range`
- `too_large` (body write exceeds 1 MiB)

---

## Extras

### Container Notes

- The image bakes `POSTIT_HOST=0.0.0.0` so podman's published-port proxy
  can reach the listener inside the container netns. Host exposure is
  governed by the `-p 127.0.0.1:8000:8000` publish flag on the `run`
  command — two distinct layers, do not conflate them.
- There is no `USER` clause in the Dockerfile. Under **rootless podman**
  the in-container uid 0 maps to the invoking user's host uid, so
  bind-mounted `~/.agent-postit` is readable and writable as your files
  with no `chown` or `--userns` ceremony. Under **system podman / docker**
  (where container root is real root), pass `--user $(id -u):$(id -g)`
  so files land as your uid.
- Final image size is ~160 MB (`python:3.12-slim-bookworm` base plus the
  `mcp` dependency tree).
- `HEALTHCHECK` runs every 30 s against `http://127.0.0.1:8000/healthz`
  (probed _inside_ the container netns, where the server is reachable on
  loopback) using `urllib` from the base image. If you change the listen
  port, override `POSTIT_PORT` and the `EXPOSE`/`-p` mapping together.

### Environment Variables

Env precedence (highest first): `--root` > `POSTIT_ROOT` > `~/.agent-postit`.
Same shape for transport / host / port: `--transport` > `POSTIT_TRANSPORT`

> `http`; `--host` > `POSTIT_HOST` > `127.0.0.1`; `--port` > `POSTIT_PORT`
> `8000`. `POSTIT_LOG` (default `-`/stderr) has no CLI flag — see
> `uv run python -m agent_postit --help` for the full surface.

A second instance trying to bind the same loopback port exits cleanly
with a "agent-postit already running on ..." message — the binary does a
preflight TCP connect check before uvicorn starts, so there is no
`EADDRINUSE` traceback and no need for a lock file.

## Editor integration

### Zed

Zed reads MCP server config from its settings file (open with `zed:
open settings file`, or edit from **Settings → AI → MCP Servers**). HTTP
servers live under `context_servers` with a `url`. The server must
already be running — Zed does not spawn it.

```jsonc
{
    "context_servers": {
        "agent-postit": {
            "url": "http://127.0.0.1:8000/mcp",
        },
    },
}
```

**Verify the server is live.** In Zed open **Settings → AI → MCP
Servers** and watch the indicator dot next to `agent-postit`. Green with
the tooltip "Server is active" means the handshake succeeded and the 13
tools have been registered. Red indicates an error; hover for details
(typically the server process is not running, or the port is wrong). A
quick independent check: `curl -s http://127.0.0.1:8000/healthz`.

If you bounce the server (rebuild, restart the container, etc.), Zed
will hold a stale session id — reload the MCP server from that same
**MCP Servers** pane to re-handshake.

No `Authorization` header is required by the server. If a client
insists on sending one (some do), a dummy `Authorization: Bearer x` is
ignored — it is cosmetic, not enforced.

#### Tool permissions (Zed)

By default Zed prompts before every tool call.
Per-tool entries use the `mcp:<server>:<tool>` key and override the
global `agent.tool_permissions.default`. Anything not listed inherits
that default.

For the full reference see the [Zed MCP guide](https://zed.dev/docs/ai/mcp)
and the [tool permissions doc](https://zed.dev/docs/agent/tool-permissions).

```jsonc
{
    "agent": {
        "tool_permissions": {
            "tools": {
                // agent-postit: auto-allow
                "mcp:agent-postit:topic.read": { "default": "allow" },
                "mcp:agent-postit:postit.read": { "default": "allow" },
                "mcp:agent-postit:postit.read_section": { "default": "allow" },
                "mcp:agent-postit:postit.read_lines": { "default": "allow" },
                "mcp:agent-postit:postit.ls": { "default": "allow" },
                "mcp:agent-postit:postit.search": { "default": "allow" },
                "mcp:agent-postit:postit.recent": { "default": "allow" },
                "mcp:agent-postit:topic.create": { "default": "allow" },
                "mcp:agent-postit:topic.write": { "default": "allow" },
                "mcp:agent-postit:postit.create": { "default": "allow" },

                // agent-postit: confirm first
                "mcp:agent-postit:postit.update_body": { "default": "confirm" },
                "mcp:agent-postit:postit.rename": { "default": "confirm" },
                "mcp:agent-postit:postit.delete": { "default": "confirm" },
            },
        },
    },
}
```

---

## stdio fallback

`--transport stdio` (`POSTIT_TRANSPORT=stdio`) makes the binary speak
JSON-RPC over stdin/stdout instead of binding a port. It is kept for
one-off sessions against a temp root and for environments without a
service manager where the client spawns the server as a child process:

```sh
uv run python -m agent_postit --transport stdio --root /tmp/scratch-notes
```

stdio and HTTP are **not** bridgeable. They are two different code paths
in the same binary: stdio uses the SDK's stdio transport and writes one
JSON-RPC frame per line; HTTP uses the Streamable HTTP transport and a
long-lived server. There is no `--transport bridge` and no plan to add
one — pick the transport that matches how your client speaks MCP. HTTP
is the default and the one you want for dogfooding.

---

## License

MIT.
