# flatnotes-mcp

English | [简体中文](docs/README_ZH.md)

A remote [MCP](https://modelcontextprotocol.io) server that lets agents read
and write one or more [flatnotes](https://github.com/dullage/flatnotes)
instances through semantic tools.

* **Multi-instance** — every tool takes an optional `server` argument; a single
  MCP endpoint fronts N flatnotes instances.
* **Credentials never reach the agent** — flatnotes username/password live in
  read-only mounted files and are only used to obtain a bearer token inside the
  MCP process.
* **Hashed inbound tokens** — the token matrix stores SHA-256 digests only.
* **Two-layer authorization** — a per-`(token, server)` scope matrix
  (`read`/`write`/`delete`) on top of flatnotes' own read-only mount boundary.

```
Agent ── Streamable HTTP + Authorization: Bearer <token> ──▶ flatnotes-mcp
                                                              │
                                        config/servers.yaml   │  instance inventory
                                        config/tokens.yaml    │  token → (server, scope) matrix
                                                              │
                                    per-server client + cached bearer (401 → re-auth)
                                                              ▼
                                        flatnotes instances (N, rw or ro mounts)
```

See `docs/PLAN.md` for the full design rationale.

## Quick start

### 1. Configure

```bash
cp .env.example .env
cp config/tokens.yaml.example config/tokens.yaml
chmod 600 config/tokens.yaml
```

Edit `config/servers.yaml` so each `url` matches the container DNS name (or the
local address) of a flatnotes instance, and give each instance a
`credentials_file`.

Create one `.env` per instance under `./creds/` (see `creds/README.md`):

```dotenv
# creds/main.env
FLATNOTES_USERNAME=youruser
FLATNOTES_PASSWORD=yourpassword
```

### 2. Create a token

```bash
./scripts/new_token.sh agent-laptop-rw "main:read,write"
```

Copy the printed `tokens.yaml` entry into `config/tokens.yaml` and store the
plaintext token in your MCP client configuration — it cannot be recovered from
the digest.

Validate everything before starting:

```bash
MCP_SERVERS_FILE=config/servers.yaml MCP_TOKENS_FILE=config/tokens.yaml \
  uv run flatnotes-mcp-check
```

### 3. Run

With Docker (single replica, `MCP_EXPOSURE=all`):

```bash
docker compose up -d --build
```

Or locally against a venv:

```bash
uv sync
MCP_SERVERS_FILE=config/servers.yaml MCP_TOKENS_FILE=config/tokens.yaml \
  uv run flatnotes-mcp
```

The MCP endpoint is `http://127.0.0.1:8800/mcp` (compose) or
`http://127.0.0.1:8080/mcp` (local). Add it to your MCP client as a remote
(Streamable HTTP) server with:

```
Authorization: Bearer <plaintext token>
```

### Splitting replicas by exposure

The commented `flatnotes-mcp-internal` service in `compose.yaml` shows the
recommended two-replica layout:

| Replica | `MCP_EXPOSURE` | `MCP_ALLOW_DELETE` | Sees |
|---|---|---|---|
| public  | `public`   | `false` | only `exposure: public` instances, no deletion |
| internal| `internal` | `true`  | all internal instances |

Both replicas read the *same* `tokens.yaml` (digests only); isolation comes
from `MCP_EXPOSURE`, from only mounting the credentials a replica needs, and
from attaching each replica to only the flatnotes networks it may reach.
`MCP_EXPOSURE=all` (the default) exposes every instance and is intended for
single-host/personal deployments.

## Configuration

### `config/servers.yaml` (non-sensitive)

| Field | Required | Description |
|---|---|---|
| `id` | yes | unique id, `[A-Za-z0-9][A-Za-z0-9_-]*`; used as the `server` argument and scope prefix |
| `url` | yes | base URL reachable from the MCP server |
| `description` | no | shown to agents by `list_servers` |
| `exposure` | no | `public` \| `internal` (default `internal`) |
| `credentials_file` | yes\* | path to the instance's flatnotes `.env` (read-only mount) |
| `capabilities` | no | subset of `read`/`write`/`delete` (default `[read]`). Must mirror the mount mode |
| `path_prefix` | no | flatnotes `FLATNOTES_PATH_PREFIX` |
| `verify_tls` | no | set `false` only for self-signed internal endpoints |

\* required the first time a tool touches that instance.

### `config/tokens.yaml` (sensitive, mode 600)

```yaml
tokens:
  - name: agent-laptop-rw
    token_sha256: "<sha256 of the plaintext token>"
    servers:
      main: [read, write]
```

Startup validation rejects: unknown server ids, unknown scopes, scopes that
exceed a server's declared `capabilities`, malformed/duplicate digests and
duplicate names.

### Environment

| Variable | Default | Description |
|---|---|---|
| `MCP_SERVERS_FILE` | `config/servers.yaml` | instance inventory |
| `MCP_TOKENS_FILE` | `config/tokens.yaml` | token matrix |
| `MCP_EXPOSURE` | `all` | `all` \| `public` \| `internal` |
| `MCP_ALLOW_DELETE` | `true` | when false the `delete_note` tool is not registered at all |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8080` / `/mcp` | bind address of the HTTP server |
| `MCP_LOG_LEVEL` | `INFO` | logging level |

## Tools

| Tool | Scope | Notes |
|---|---|---|
| `list_servers()` | — | instances visible to this token + allowed operations |
| `search_notes(term, server?, sort?, order?, limit?)` | read | `term` supports `#tag`, `tag:name`, `"phrases"` |
| `list_recent_notes(limit?, server?)` | read | newest first |
| `list_tags(server?)` | read | |
| `get_note(title, server?)` | read | full Markdown content |
| `create_note(title, content?, server?)` | write | |
| `update_note(title, new_title?, new_content?, server?)` | write | rename and/or overwrite |
| `append_note(title, text, server?)` | read + write | fetch, then append |
| `upload_attachment(filename, content_b64, server?, content_type?)` | write | |
| `download_attachment(filename, server?)` | read | returns base64 |
| `delete_note(title, server?)` | delete | only when `MCP_ALLOW_DELETE=true` |

Resources: `servers://list` and `note://{server}/{title}`.

### Choosing the `server`

`allowed = token's servers ∩ this replica's MCP_EXPOSURE`. If `allowed` has
exactly one entry, `server` may be omitted; otherwise it is required and the
error lists the available ids. An explicit `server` must be in `allowed`.

## Security model

1. **flatnotes mount (hard boundary)** — an instance mounted `:ro` refuses
   writes itself; MCP cannot override that.
2. **MCP scopes** — every tool checks the required operation against the
   token's per-server scopes; scopes are validated against `capabilities` at
   startup.
3. **Replica limits** — `MCP_EXPOSURE` hides instances and `MCP_ALLOW_DELETE`
   removes the delete capability entirely.

Inbound tokens are compared by SHA-256 digest in constant time. flatnotes
credentials and bearer tokens are never logged or returned by a tool.

## Development

```bash
uv sync
uv run pytest
```

The test suite covers configuration validation, the flatnotes HTTP client
(against a mock transport) and the MCP server end-to-end over an in-process
Streamable HTTP app (authentication, the scope matrix, `MCP_EXPOSURE`,
`MCP_ALLOW_DELETE` and every tool).

### Manual verification

```bash
# 1. no token → 401
curl -i -X POST http://127.0.0.1:8800/mcp

# 2. with a token → initialize + tools/list
#    (see docs/PLAN.md §6 for the full checklist: per-server scopes,
#     read-only enforcement, exposure filtering, delete gating)
```
