# Commands

Quick reference for build, run, test, and inspecting the server when picking
up the project cold. All commands assume you `cd` to the repo root first and
have Python 3.12+ and [`uv`](https://github.com/astral-sh/uv) installed.

The user-facing run recipes (containers, systemd quadlet, stdio fallback)
live in [`../README.md`](../README.md) under "Build, install, run". This page
is the dev-loop counterpart: the commands you actually run while hacking on
the code.

---

## Environment / setup

```sh
uv sync                 # install all deps (incl. dev group: pytest, pytest-asyncio) into ./.venv
uv sync --frozen        # same, but refuse to update uv.lock (CI / reproducible)
uv lock --upgrade       # refresh uv.lock against the latest compatible releases
```

The venv is `.venv/`. There is no separate test extras; the `dev` dependency
group (see `pyproject.toml`) is pulled in by plain `uv sync`.

---

## Running the server (dev)

Default transport is **Streamable HTTP** on `127.0.0.1:8000`, data root
`~/.agent-postit/`. Override via flags **or** env (flag wins; env vars are
`POSTIT_ROOT` / `POSTIT_TRANSPORT` / `POSTIT_HOST` / `POSTIT_PORT`):

```sh
uv run python -m agent_postit                                   # defaults
uv run python -m agent_postit --root ./my-notes --port 8011     # flags
POSTIT_ROOT=/tmp/scratch-notes POSTIT_PORT=8011 uv run python -m agent_postit   # env
uv run python -m agent_postit --transport stdio --root /tmp/scratch-notes       # stdio one-off
```

`--transport` accepts `http` (default), `stdio`, and the alias spellings
`streamable_http` / `streamable-http`. Anything else exits non-zero with a
clear message (see `_resolve_transport` in `__main__.py`).

A running server logs to stdout (uvicorn). Stop it with `Ctrl-C`, or by
killing the pid listened on `127.0.0.1:8000`:

```sh
pkill -f "python -m agent_postit"           # if you launched it manually
systemctl --user stop agent-postit.service   # if it's running under the quadlet
```

---

## Liveness probes (no client needed)

```sh
curl -s http://127.0.0.1:8000/healthz        # → ok   (the liveness endpoint)
curl -s http://127.0.0.1:8000/mcp -i         # → 405/406 unless correct MCP framing; useful to confirm the listener is MCP-shaped
```

`/healthz` is the only path that does not require MCP framing — use it to tell
"server is up" apart from "MCP handshake works". They are different failure
modes; see *Pitfalls* below.

---

## Tests

The suite is plain `pytest`, configured in `pyproject.toml`
(`tool.pytest.ini_options` → `testpaths = ["tests"]`, `addopts = "-q"`).
`test_http_smoke.py` spawns a real uvicorn instance on a free loopback port,
so it exercises the full HTTP stack, not just handlers in isolation.

```sh
uv run pytest                                # full suite (~5s, 8 files)
uv run pytest -v                            # verbose: one line per test
uv run pytest tests/test_store.py            # one file
uv run pytest tests/test_http_smoke.py -k smoke   # by name/kw
uv run pytest --collect-only -q             # list what would run, don't execute
uv run pytest -x                            # stop on first failure
uv run pytest -p no:asyncio                 # disable the plugin if you suspect an asyncio hiccup
```

As of this writing the suite is 176 tests across 9 files and runs in ~6s on a
warm venv. The split:

| File                      | What it covers |
|---------------------------|----------------|
| `test_store.py`           | Note CRUD, on-disk layout, `TOPIC` reservation, case-folding |
| `test_paths.py`           | Path resolution, traversal guards, env/flag precedence |
| `test_search.py`          | Regex search over names + bodies, recursive flag |
| `test_sections.py`        | `postit.read_section` / `postit.read_lines` slicing |
| `test_server.py`          | Tool dispatch, `ToolError` returns, arg validation |
| `test_http_smoke.py`      | Real uvicorn: `initialize` framing, `json_response=True` mode |
| `test_concurrency.py`     | Concurrent writes/readers under the file store |
| `test_logging.py`         | Log routing, redaction if any |
| `test_capabilities.py`    | `postit.capabilities` surface + effect-hint accuracy |

---

## Packaging / build

```sh
uv build                  # produce sdist + wheel into dist/ (uses hatchling backend)
uv run agent-postit --help # same as `python -m agent_postit --help` once installed
```

The console-script entrypoint `agent-postit` (declared in
`pyproject.toml` → `project.scripts`) points at `agent_postit.__main__:main`,
so `agent-postit ...` and `python -m agent_postit ...` are identical.

---

## Container (smoke, not production)

The README has the long form; the short version for "is the image still
buildable":

```sh
podman build --format docker -t agent-postit:latest .   # use --format docker so HEALTHCHECK is honored
podman run --rm -p 127.0.0.1:8000:8000 -v ~/.agent-postit:/data:Z agent-postit:latest
curl -s "$(podman inspect -l -f '{{.NetworkSettings.IPAddress}}')/healthz" || true
```

`Containerfile` is a symlink to `Dockerfile` (podman convention). Don't drop
the `127.0.0.1:` prefix on `-p` and never add `-i` / `-t` for the HTTP
transport; it has no TTY.

---

## Pitfalls (read these once)

- **"Healthy server" ≠ "tools registered".** `curl /healthz` returning `ok`
  only proves the process is listening. Whether an MCP client (Zed, etc.)
  actually saw the 15 tools advertised is a separate question, answered
  client-side. If a client reports tools missing, the server being up is not
  evidence against the bug — check the client's MCP indicator and start a
  fresh chat; tool lists are cached per-session and not retroactively
  refreshed when settings change.

- **Stale session id after a server bounce.** A client that connected, then
  the server restarted, will keep retrying a session id the server no longer
  knows about. Symptom: tools were there, now silently gone. Fix per the
  README §Zed: reload the MCP server from the editor's MCP pane, then open a
  new chat.

- **`POSTIT_PORT` must parse as int.** A non-integer value exits non-zero
  with `POSTIT_PORT=... is not an integer` (see `_resolve_port`); it does not
  silently fall back to 8000.

- **`--transport bridge` does not exist.** stdio and HTTP are two code paths
  in the same binary and are not bridgeable. Pick the one that matches how
  your client speaks MCP; HTTP is the default and the right choice for
  dogfooding.