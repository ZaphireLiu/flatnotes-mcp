# flatnotes-mcp

[English](../README.md) | 简体中文

一个远程 [MCP](https://modelcontextprotocol.io) 服务器，让 agent 通过语义化工具读取和写入一个或多个 [flatnotes](https://github.com/dullage/flatnotes) 实例。

* **多实例** —— 每个工具都接受一个可选的 `server` 参数；单个 MCP 端点即可对接 N 个 flatnotes 实例。
* **凭据不会到达 agent** —— flatnotes 的用户名/密码存放在只读挂载的文件中，只在 MCP 进程内部用于获取 bearer token。
* **入站令牌以哈希形式存储** —— 令牌矩阵只保存 SHA-256 摘要。
* **两层授权** —— 在 flatnotes 自身的只读挂载边界之上，还有按 `(token, server)` 的权限矩阵（`read`/`write`/`delete`）。

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

完整的设计理由见 `docs/PLAN.md`。

## 快速开始

### 1. 配置

```bash
cp .env.example .env
cp config/tokens.yaml.example config/tokens.yaml
chmod 600 config/tokens.yaml
```

编辑 `config/servers.yaml`，使每个 `url` 都指向某个 flatnotes 实例的容器 DNS 名称（或本地地址），并为每个实例指定一个 `credentials_file`。

在 `./creds/` 下为每个实例创建一个 `.env` 文件（参见 `creds/README.md`）：

```dotenv
# creds/main.env
FLATNOTES_USERNAME=youruser
FLATNOTES_PASSWORD=yourpassword
```

### 2. 创建令牌

```bash
./scripts/new_token.sh agent-laptop-rw "main:read,write"
```

把输出的 `tokens.yaml` 条目复制到 `config/tokens.yaml` 中，并将明文令牌保存在你的 MCP 客户端配置里 —— 它无法从摘要中恢复。

启动前先做一次完整校验：

```bash
MCP_SERVERS_FILE=config/servers.yaml MCP_TOKENS_FILE=config/tokens.yaml \
  uv run flatnotes-mcp-check
```

### 3. 运行

使用 Docker（单副本，`MCP_EXPOSURE=all`）：

```bash
docker compose up -d --build
```

或者在本地针对 venv 运行：

```bash
uv sync
MCP_SERVERS_FILE=config/servers.yaml MCP_TOKENS_FILE=config/tokens.yaml \
  uv run flatnotes-mcp
```

MCP 端点为 `http://127.0.0.1:8800/mcp`（compose）或 `http://127.0.0.1:8080/mcp`（本地）。将其作为远程（Streamable HTTP）服务器添加到你的 MCP 客户端，并附带：

```
Authorization: Bearer <plaintext token>
```

### 按暴露范围拆分副本

`compose.yaml` 中被注释掉的 `flatnotes-mcp-internal` 服务展示了推荐的双副本布局：

| 副本 | `MCP_EXPOSURE` | `MCP_ALLOW_DELETE` | 可见范围 |
|---|---|---|---|
| public  | `public`   | `false` | 只能看到 `exposure: public` 的实例，不可删除 |
| internal| `internal` | `true`  | 所有内部实例 |

两个副本读取**同一份** `tokens.yaml`（仅含摘要）；隔离来自于 `MCP_EXPOSURE`、只挂载该副本所需的凭据，以及只把每个副本接入它被允许访问的 flatnotes 网络。
`MCP_EXPOSURE=all`（默认值）会暴露所有实例，适用于单主机/个人部署。

## 配置

### `config/servers.yaml`（非敏感）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一 id，格式 `[A-Za-z0-9][A-Za-z0-9_-]*`；用作 `server` 参数和 scope 前缀 |
| `url` | 是 | 从 MCP 服务器可访问的 base URL |
| `description` | 否 | 由 `list_servers` 展示给 agent |
| `exposure` | 否 | `public` \| `internal`（默认 `internal`） |
| `credentials_file` | 是\* | 该实例的 flatnotes `.env` 路径（只读挂载） |
| `capabilities` | 否 | `read`/`write`/`delete` 的子集（默认 `[read]`）。必须与挂载模式一致 |
| `path_prefix` | 否 | flatnotes 的 `FLATNOTES_PATH_PREFIX` |
| `verify_tls` | 否 | 仅在自签名的内部端点下设为 `false` |

\* 首次有工具访问该实例时必填。

### `config/tokens.yaml`（敏感，权限 600）

```yaml
tokens:
  - name: agent-laptop-rw
    token_sha256: "<sha256 of the plaintext token>"
    servers:
      main: [read, write]
```

启动时的校验会拒绝：未知的 server id、未知的 scope、超出 server 所声明 `capabilities` 的 scope、格式错误或重复的摘要，以及重复的名称。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_SERVERS_FILE` | `config/servers.yaml` | 实例清单 |
| `MCP_TOKENS_FILE` | `config/tokens.yaml` | 令牌矩阵 |
| `MCP_EXPOSURE` | `all` | `all` \| `public` \| `internal` |
| `MCP_ALLOW_DELETE` | `true` | 为 false 时完全不注册 `delete_note` 工具 |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8080` / `/mcp` | HTTP 服务器的绑定地址 |
| `MCP_LOG_LEVEL` | `INFO` | 日志级别 |

## 工具

| 工具 | Scope | 说明 |
|---|---|---|
| `list_servers()` | — | 该令牌可见的实例 + 允许的操作 |
| `search_notes(term, server?, sort?, order?, limit?)` | read | `term` 支持 `#tag`、`tag:name`、`"phrases"` |
| `list_recent_notes(limit?, server?)` | read | 最新的在前 |
| `list_tags(server?)` | read | |
| `get_note(title, server?)` | read | 完整的 Markdown 内容 |
| `create_note(title, content?, server?)` | write | |
| `update_note(title, new_title?, new_content?, server?)` | write | 重命名和/或覆盖 |
| `append_note(title, text, server?)` | read + write | 先获取，再追加 |
| `upload_attachment(filename, content_b64, server?, content_type?)` | write | |
| `download_attachment(filename, server?)` | read | 返回 base64 |
| `delete_note(title, server?)` | delete | 仅在 `MCP_ALLOW_DELETE=true` 时可用 |

资源：`servers://list` 和 `note://{server}/{title}`。

### 如何选择 `server`

`allowed = 令牌的 servers ∩ 本副本的 MCP_EXPOSURE`。如果 `allowed` 恰好只有一项，可以省略 `server`；否则必须提供，且错误信息会列出可用的 id。显式指定的 `server` 必须属于 `allowed`。

## 安全模型

1. **flatnotes 挂载（硬边界）** —— 以 `:ro` 挂载的实例自身就会拒绝写入，MCP 无法覆盖这一点。
2. **MCP 权限范围** —— 每个工具都会按令牌的 per-server scope 检查所需操作；scope 在启动时还会对照 `capabilities` 进行校验。
3. **副本限制** —— `MCP_EXPOSURE` 隐藏实例，`MCP_ALLOW_DELETE` 则彻底移除删除能力。

入站令牌以恒定时间比较 SHA-256 摘要。flatnotes 凭据和 bearer token 永远不会被写入日志，也不会由工具返回。

## 开发

```bash
uv sync
uv run pytest
```

测试套件覆盖配置校验、flatnotes HTTP 客户端（针对 mock transport）以及 MCP 服务器的端到端流程（基于进程内的 Streamable HTTP 应用：认证、权限矩阵、`MCP_EXPOSURE`、`MCP_ALLOW_DELETE` 以及每个工具）。

### 手动验证

```bash
# 1. 不带令牌 → 401
curl -i -X POST http://127.0.0.1:8800/mcp

# 2. 带令牌 → initialize + tools/list
#    （完整清单见 docs/PLAN.md §6：per-server scope、
#      只读强制、exposure 过滤、删除开关）
```
