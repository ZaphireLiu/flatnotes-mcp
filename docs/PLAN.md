# PLAN：flatnotes MCP Server 开发方案（多实例 / 配置化 Token）

> 状态：**已实施**（本方案已落地为可运行的 MCP Server；实现偏差见 §10）
> 拟定日期：2026-10-14
> 最近修订：
> - 2026-10-14（新增**多 flatnotes 实例**支持：配置化任意 token、per-(token, server) scope 矩阵、`list_servers` 与 `server` 参数）
> - 2026-10-16（完成开发：`src/flatnotes_mcp/*`、容器化、测试套件）

本文件聚焦 **MCP Server 自身的设计与开发**：数据模型、鉴权、工具、配置、源码结构与容器化。个人系统的对外发布（公网入口、SSO 网关、隧道转发等）不在本文范围。

---

## 1. 背景与目标

flatnotes 是无数据库、纯 Markdown 的笔记应用，官方**没有** MCP，也无内置 `/mcp` 端点（实测 404）。目标是自建一个 MCP Server，让 Agent 以语义化工具访问/操作 flatnotes，并把认证与权限边界封装在 MCP 内部——**密码、Token 永不进入 Agent 上下文**。

**多实例目标**：预计部署多个 flatnotes 实例，MCP 需同时服务它们。多实例用于两件事：

1. **异质文档隔离**：不同性质的文档挂不同文件夹、起不同实例，各实例搜索/标签空间互不干扰。
2. **同一文件夹的权限分层**：同一文件夹挂进多个实例，靠**容器挂载模式**区分能力（rw 实例=可写、ro 实例=只读），从而绕开 flatnotes 单用户、无多用户的限制——即"从 flatnotes 侧做权限管理"。

已锁定的关键决策：

| 决策项 | 选择 |
|---|---|
| 实现方式 | 定制 FastMCP（方案 A），非 OpenAPI 自动生成、非文件系统直读写 |
| 传输 | 远程 HTTP（Streamable HTTP），一个端点服务多客户端 |
| 运行形态 | Docker 容器（`compose.yaml` 组织），可多副本按暴露面拆分 |
| 多实例 | 支持 N 个 flatnotes 实例；所有工具带 `server` 参数 |
| Token 管理 | **配置文件记录**（任意数量），**SHA-256 哈希**存储 |
| 权限模型 | per-(token, server) scope 矩阵；实例 ro/rw 挂载为**硬边界**，MCP scope 在能力内再细分 |
| 暴露控制 | 可用 `MCP_EXPOSURE` 过滤实例、用 `MCP_ALLOW_DELETE` 裁剪删除能力 |

---

## 2. flatnotes 侧事实（开发依据）

| 项 | 值 |
|---|---|
| 应用性质 | 无数据库、纯 Markdown 笔记；笔记即文件 |
| 认证 | `FLATNOTES_AUTH_TYPE=password`，**单用户**；用户名 + 密码由实例的 `.env` 提供 |
| 数据目录 | 笔记 markdown + `.flatnotes/` 搜索索引（同一目录） |
| 部署单位 | 每个实例一个容器、一份数据目录、一个 docker 网络（`<实例>_default`） |

> **关键约束**：flatnotes 无多用户。跨用户的"权限管理"只能靠**多实例 + 挂载模式（rw/ro）**实现（见 §1、§4）。

**API 面积（11 个端点，无"列出笔记"端点，列表需 `GET /api/search?term=*`）：**

```
POST /api/token              GET /api/auth-check      GET /api/config
POST /api/notes              GET /api/notes/{title}
PATCH /api/notes/{title}     DELETE /api/notes/{title}
GET /api/search              GET /api/tags
POST /api/attachments        GET /api/attachments/{filename}      GET /health
```

---

## 3. 总体架构

```
 Agent ── Streamable HTTP + Authorization: Bearer <token> ──▶ MCP Server
                                                               │
                                     config/servers.yaml  (实例清单)
                                     config/tokens.yaml   (token→(server,scope) 矩阵)
                                                               │  按矩阵授权 + 逐 server 出站换 Bearer
                                                               ▼
                              flatnotes 实例们（N 个；可共享同一文件夹或分属不同文件夹）
```

- MCP 为每个 server 维护一个 HTTP 客户端：各自的 base URL、凭据、Bearer 缓存与 401 重取。
- 可按暴露面把 MCP 跑成**多个副本**（例如"对外档"只接部分实例并禁 `delete`，"内网档"接全部），通过 `MCP_EXPOSURE` / `MCP_ALLOW_DELETE` 与网络隔离区分；本文只描述能力，不涉及具体对外发布。

**两层鉴权**：
- 入站：MCP 自有 token（Agent 持有）。token 为 256 位随机，配置中以 SHA-256 存储；按 `token → (server, scope)` 矩阵授权。
- 出站：MCP 用各实例账号密码换 Bearer，**按 server 分别**缓存、401 重取。凭据从只读挂载的各实例 `.env` 读取，**不进任何客户端配置**。

---

## 4. 权限模型（多实例 token 矩阵）

权限由**三层**共同决定：

1. **实例挂载能力（硬边界，flatnotes 侧）**：如某实例以 `:ro` 挂载数据目录，其写/删由 flatnotes 本体拒绝。这是最终兜底，MCP 无法绕过。
2. **token per-(server) scope（MCP 强制）**：每个 token 对每个 server 声明可用操作 `read` / `write` / `delete`，必须 ⊆ 该 server 声明的 `capabilities`；MCP 在工具内校验。
3. **副本层兜底**：`MCP_EXPOSURE` 只暴露允许的实例、`MCP_ALLOW_DELETE=false` 一律不提供删除。

### 4.1 配置模型

`config/servers.yaml`（**非敏感**）——每个 flatnotes 实例一条，`capabilities` 反映其挂载模式：

```yaml
servers:
  - id: main
    url: http://flatnotes:8080
    description: 通用笔记（主实例）
    exposure: public            # public | internal
    credentials_file: /creds/main.env
    capabilities: [read, write, delete]

  - id: docs
    url: http://flatnotes-docs:8080
    description: 大部头文档隔离库（异质文档）
    exposure: internal
    credentials_file: /creds/docs.env
    capabilities: [read, write, delete]

  - id: main-ro
    url: http://flatnotes-main-ro:8080
    description: 通用笔记（只读视图，与 main 同文件夹）
    exposure: internal
    credentials_file: /creds/main-ro.env
    capabilities: [read]        # 该实例以 :ro 挂数据目录，flatnotes 侧天然拒写
```

`config/tokens.yaml`（**600**，加入 `.gitignore`）——token→server→scopes 矩阵，**只存哈希**：

```yaml
tokens:
  - name: agent-public-ro
    token_sha256: "<openssl rand -hex 32 | sha256>"
    servers: { main: [read] }

  - name: agent-laptop-rw
    token_sha256: "<...>"
    servers: { main: [read, write] }

  - name: admin-internal
    token_sha256: "<...>"
    servers: { main: [read, write, delete], main-ro: [read], docs: [read, write, delete] }
```

**启动校验（关键）**：引用未定义的 `server id`、或某 server 的 scopes ⊄ 其 `capabilities`，一律报错退出。避免"硬边界"与"声明能力"自相矛盾。

### 4.2 鉴权实现

- **自定义 `TokenVerifier`**：对入站 token 求 `sha256`，与 `tokens.yaml` 的键比对；命中则把矩阵展开成 scope 字符串列表（`"main:read"`, `"docs:write"`, …）放进 `AccessToken.scopes`。
- 工具内用 `get_access_token()` 读取 scope 判定；若所用 FastMCP 版本不暴露 token，则退化为 ASGI 中间件解析 `Authorization: Bearer` 存入 contextvar。
- **仅存哈希**：配置文件即便泄漏也不能直接复用 token。代价是明文 token 无法从配置恢复，**创建时必须自行留存**（密码管理器 / 客户端配置，OpenCode 用 `{env:...}`）。

### 4.3 `server` 参数解析规则

设 `allowed = token 可访问的实例 ∩ 当前副本 `MCP_EXPOSURE` 允许的实例`：

- **省略 `server`**：若 `allowed` 恰为 1 个 → 用它；否则报错并列出可选 `id`。
- **显式传入**：必须 ∈ `allowed`，否则"未知或未授权"。多实例下同名笔记可能重复，故须显式带 `server` 的场景由上述规则自然覆盖。

### 4.4 工具 ↔ flatnotes API 映射

| Tool | 映射 | 所需 scope |
|---|---|---|
| `list_servers()` | 本地（据 token + exposure 过滤） | — |
| `search_notes(server?, term, sort?, order?, limit?)` | `GET /api/search` | read |
| `list_recent_notes(server?, limit?)` | `GET /api/search?term=*&sort=lastModified&order=desc` | read |
| `list_tags(server?)` | `GET /api/tags` | read |
| `get_note(server?, title)` | `GET /api/notes/{title}` | read |
| `create_note(server?, title, content)` | `POST /api/notes` | write |
| `update_note(server?, title, new_title?, new_content?)` | `PATCH /api/notes/{title}` | write |
| `append_note(server?, title, text)` | GET + PATCH（便利组合） | write |
| `upload_attachment(server?, filename, content_b64)` | `POST /api/attachments` | write |
| `delete_note(server?, title)` | `DELETE /api/notes/{title}` | delete（且副本 `MCP_ALLOW_DELETE`） |

- `list_servers()` 返回当前 token 可见实例的 `{id, description, operations}`，供 Agent 决策。
- 可选加分项：Resources（`note://{server}/{title}`、`servers://list`）、Prompt（如"总结某标签下笔记"）。

---

## 5. 组件与目录

```
flatnotes-mcp/
├── compose.yaml
├── .env                 # 非敏感覆盖（可留空）
├── Dockerfile
├── config/
│   ├── servers.yaml     # 实例清单（非敏感）
│   └── tokens.yaml      # token→(server,scope) 矩阵（600，敏感，gitignore）
└── src/flatnotes_mcp/
    ├── __init__.py
    ├── config.py        # 载入 servers.yaml / tokens.yaml 并校验
    ├── server.py        # FastMCP 实例、HashedTokenVerifier、工具定义
    └── client.py        # 每 server 一个 flatnotes 客户端（token 缓存、401 重试）
```

### 5.1 `compose.yaml`（示意，端口/网络按实际部署填）

```yaml
services:
  flatnotes-mcp:                     # 对外档（可选拆分）
    build: .
    container_name: flatnotes-mcp
    restart: unless-stopped
    env_file: [.env]
    environment:
      MCP_SERVERS_FILE: /config/servers.yaml
      MCP_TOKENS_FILE: /config/tokens.yaml
      MCP_EXPOSURE: public           # 只暴露 exposure=public 的实例
      MCP_ALLOW_DELETE: "false"      # 一律不提供 delete
    ports:
      - "127.0.0.1:<对外端口>:8080"
    volumes:
      - ./config:/config:ro
      # 仅挂该副本可访问实例的凭据；未挂的实例即便配置写了也读不到
      - <实例项目>/.env:/creds/main.env:ro
    networks:
      - flatnotes_default            # main（public 实例）

  flatnotes-mcp-internal:            # 内网档（可选拆分）
    build: .
    container_name: flatnotes-mcp-internal
    restart: unless-stopped
    env_file: [.env]
    environment:
      MCP_SERVERS_FILE: /config/servers.yaml
      MCP_TOKENS_FILE: /config/tokens.yaml
      MCP_EXPOSURE: internal
      MCP_ALLOW_DELETE: "true"
    volumes:
      - ./config:/config:ro
      - <实例项目>/main.env:/creds/main.env:ro
      - <实例项目>/docs.env:/creds/docs.env:ro
      - <实例项目>/main-ro.env:/creds/main-ro.env:ro
    networks:
      - flatnotes_default
      - flatnotes-docs_default
      - flatnotes-main-ro_default

networks:
  flatnotes_default:         { external: true }
  flatnotes-docs_default:    { external: true }
  flatnotes-main-ro_default: { external: true }
```

> 网络名以各 flatnotes 实例 compose 项目实际生成的 `<project>_default` 为准（`docker network ls` 核对）。
> 两副本读**同一份** `tokens.yaml`（只含哈希）；隔离靠 `exposure` 过滤 + 网络不互通 + 凭据只挂可访问实例。若需更强隔离，可拆成 `tokens-public.yaml` / `tokens-internal.yaml`。

### 5.2 Token 生成与登记

```bash
T=$(openssl rand -hex 32)          # 256 位明文 token，交给客户端持有
H=$(printf '%s' "$T" | sha256sum | cut -d' ' -f1)   # 写入 tokens.yaml 的 token_sha256
```

明文 `T` 立即登记到密码管理器 / 客户端配置（`{env:...}`），配置文件只保留 `H`。

### 5.3 `Dockerfile`

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY src ./src
EXPOSE 8080
CMD ["uv", "run", "flatnotes-mcp"]
```

---

## 6. 验证步骤（MCP 功能）

1. 容器启动后，未带 token 请求 `/mcp` → 401。
2. 带合法 token：`initialize` → `tools/list` → `list_servers` 返回正确实例子集（含 `description` / `operations`）。
3. `search_notes` 检索 → `create_note` 写入 → `get_note` 读回。
4. 分档与多实例：
   - 只授权单实例的 token：省略 `server` 成功；多实例 token 省略 `server` 报错并列出可选 id。
   - read-only token 调 `create_note` ❌（权限拒绝）；read/write token ✅。
   - **硬边界**：rw 实例可写、对应 ro 实例写操作被 flatnotes 拒绝（即便 MCP scope 误放开也拦得住）。
   - `delete_note`：`MCP_ALLOW_DELETE=false` 的副本不可用；`true` 的副本且 token 有 delete scope 时 ✅。
5. `MCP_EXPOSURE` 过滤：某副本看不到 `exposure` 不匹配的实例。

---

## 7. 实施清单（建议顺序）

1. [x] 定稿实例清单（id / 数据目录 / rw-ro 分层 / 各自 URL），写入 `config/servers.yaml`。
2. [x] 生成 token 明文 + SHA-256，写 `config/tokens.yaml`（600）并登记明文；`.gitignore` 忽略该文件。
3. [x] 建 `Dockerfile` + `src/flatnotes_mcp/*`（`config.py` 校验、`server.py` 鉴权与工具、`client.py` 多实例客户端）。
4. [x] `docker compose up -d --build`（`compose.yaml` 提供默认单副本 + 可拆分的双副本示例）。
5. [x] 本地验证（§6 全部用例，见 `tests/` 与 §10）。

---

## 8. 运维与回滚

- 升级：`docker compose build --pull && docker compose up -d`。
- 备份：**项目目录**（compose / `config/` / 源码）+ 各 flatnotes 数据目录；MCP 本身无持久化状态。
- 增删实例：改 `config/servers.yaml` + `tokens.yaml`、补挂凭据与网络，`docker compose up -d` 重建。
- 回滚：`docker compose down` 即可，**不影响 flatnotes 本体**。

---

## 9. 未决 / 风险

- **只读实例 × 搜索索引**：flatnotes 会在数据目录的 `.flatnotes/` 写搜索索引。`:ro` 挂载下索引能否写入/是否陈旧需**实测**；若只读实例起不来或搜索失效，"ro 分层"需改为"仍 rw 挂载、由 MCP scope 拦写"。
- **同文件夹多实例的索引竞争**：勿让两个 **rw** 实例同时挂同一文件夹写索引；建议仅一个 rw 属主，其余 ro。
- `capabilities` 由 `servers.yaml` **人工声明**（MCP 无法探测挂载模式），依赖启动校验 scope ⊆ capabilities 保持一致。
- 哈希存储意味着明文 token 不可从配置恢复，创建/轮换时必须同步更新客户端。
- FastMCP 自定义 `TokenVerifier` 与客户端 OAuth 发现的兼容性需实测；不兼容则改用 ASGI 中间件方案。
- `latest` 镜像若升级导致 API 字段变化，需同步更新 MCP 封装。
- 新 flatnotes 实例的部署**不在本计划范围**。

---

## 10. 实现说明与偏差

实现位于 `src/flatnotes_mcp/`：`config.py`（载入 + 校验）、`client.py`（每实例客户端）、`server.py`（鉴权、工具、Resource）。

与本文的差异：

- **FastMCP 4.x**：实际采用 `fastmcp>=2.11`（当前解析到 4.1.0）。传输用 `transport="http"`（即 Streamable HTTP），端点 `MCP_PATH`（默认 `/mcp`）。自定义 `TokenVerifier.verify_token` 返回 `AccessToken`，工具内用 `get_access_token()` 读取 scope——§4.2 的"退化 ASGI 中间件"方案未启用。
- **`MCP_EXPOSURE` 默认 `all`**：默认不过滤（单机/个人部署更方便）；对外分档时显式设 `public` / `internal`。
- **`append_note` 需要 `read` + `write`**（先 GET 再 PATCH），比 §4.4 表格里的单 `write` 更严格。
- **新增 `download_attachment` 工具**（`GET /api/attachments/{filename}` 的封装，返回 base64），与 `upload_attachment` 配对。
- **`Dockerfile` 分层修正**：先 `uv sync --no-install-project` 装依赖，再拷 `src` 安装项目本体（原示意写法在项目源码拷入前就 `uv sync`，会构建失败）。
- **`compose.yaml`** 默认只有一个服务（`MCP_EXPOSURE=all`，挂载 `./config` 与 `./creds` 目录）；计划中的"公共档 / 内网档"双副本以注释形式给出，需按实际网络名开启。
- **凭证来源**：仅读取实例 `.env` 中的 `FLATNOTES_USERNAME` / `FLATNOTES_PASSWORD`；TOTP（`FLATNOTES_AUTH_TYPE=totp`）暂不支持。
- **CLI**：除 `flatnotes-mcp` 外提供 `flatnotes-mcp-check`（校验配置并打印摘要，供 CI / 启动前使用）。

测试（`uv run pytest`，45 例）：

- `tests/test_config.py`：YAML 解析与 §4.1 的全部启动校验。
- `tests/test_client.py`：基于 `httpx.MockTransport` 的登录缓存、401 重取、错误透传、URL 编码、附件上传。
- `tests/test_server.py`：进程内 ASGI Streamable HTTP 端到端，覆盖 401、token 矩阵、`server` 解析、per-server scope、`MCP_EXPOSURE`、`MCP_ALLOW_DELETE`、各工具与 Resource。


