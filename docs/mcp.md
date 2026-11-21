# Superset MCP 服务：开发与公网上线要求

> 覆盖：MCP 服务架构与工具目录、本机开发模式启用、AI 客户端接入、公网上线安全要求与检查清单。
> 结论：**开发模式三步启用（dev 用户 + 端口映射 + `superset mcp run`），仅限本机**；
> 公网上线必须完成五项改造（JWT 认证、专用低权限账号、TLS 反代收口、代理层限流、正式版镜像），
> 开发模式与 RC 镜像都不出内网。
> 版本锚定：Superset 7.0.0rc3 + fastmcp 3.4.11（镜像预装），行为实测于 2026-11-21；
> 官方要求 Superset 5.0+，MCP 服务自 6.1 起官方集成。

## 1. 架构与实测基线

MCP（Model Context Protocol）服务让 Claude、ChatGPT、OpenCode 等 MCP 客户端以自然语言
操作 Superset：查询数据、建图表、组仪表盘、存 SQL。官方实现内置于 Superset 代码库
（`superset/mcp_service/`，基于 FastMCP），以**独立进程**运行：

```text
AI 客户端 ──MCP(HTTP+JSON-RPC)──> MCP Server(:5008/mcp) ──共享──> 元数据库(PostgreSQL)
                                                    └──────────> 同一份 superset_config.py
```

架构要点（实测确认）：

| 项 | 状态 | 说明 |
|---|---|---|
| `superset/mcp_service/` 模块 | 镜像自带 | 7.0.0rc3 容器内存在 |
| fastmcp 依赖 | 镜像预装 3.4.11 | 无需额外 pip 安装 |
| `superset mcp` CLI | 可用 | `superset mcp run --host <h> --port <p>` |
| 配置加载 | 与 web 同源 | 启动日志出现 `Loaded your LOCAL configuration`——`engine_patches.py`（DuckDB 三层闸门 + odps 方言补丁）对 MCP 进程同样生效 |
| 权限模型 | RBAC + RLS | 所有工具调用经 auth hook 加载用户进 Flask `g.user`，DAO 层自动套用与 web UI 相同的权限与行级安全（官方文档口径） |

进程关系：MCP server 与 web server（8088）互不替代，共享同一元数据库与配置文件，
可独立启停、独立升级。

## 2. 工具目录

按域分类（官方文档口径，共 18+ 个）：

| 域 | 工具 | 说明 |
|---|---|---|
| 数据集 | `list_datasets` / `get_dataset_info` | 列举与查看列/指标元数据，支持缓存控制 |
| SQL | `execute_sql` | 执行查询，自动套 RBAC/RLS，支持 Jinja 模板参数 |
| SQL | `save_sql_query` / `open_sql_lab_with_context` / `generate_explore_link` | 存查询 / 带 SQL 状态打开 SQL Lab / 生成 Explore 链接 |
| 图表 | `list_charts` / `get_chart_info` / `get_chart_data` / `get_chart_preview` | 列举、详情、取数、PNG 预览 |
| 图表 | `generate_chart` / `update_chart` / `update_chart_preview` | 建图（可存）、改配置、改前预览 |
| 仪表盘 | `list_dashboards` / `get_dashboard_info` / `generate_dashboard` / `add_chart_to_existing_dashboard` | 列举、布局详情、从图表 ID 组盘、追加图表 |
| 数据库 | `list_databases` / `get_database_info` | 连接清单与详情 |
| 实例 | `get_instance_info` | 实例统计 |

写操作（mutate 类工具）委托给 Superset 现有 chart 命令，不绕过图表编辑权限；
外部系统管理的仪表盘拒绝布局/成员/删除类修改（官方文档口径）。

## 3. 开发模式启用（仅限本机）

### 3.1 三步启用

**① `docker/superset_config.py` 配置 dev 用户**：

```python
# MCP 服务开发模式：所有操作以该用户执行（该用户必须已存在于 Superset）
# 仅限本机环境，公网部署必须删除并改用 JWT（见第 4 节）
MCP_DEV_USERNAME = "admin"
```

**② `docker-compose.yml` app 服务加端口映射**：

```yaml
    ports:
      - "8088:8088"
      - "5008:5008"   # MCP
```

**③ 启动服务**（实测 30 秒内就绪，日志出现 `Uvicorn running on http://0.0.0.0:5008`）：

```bash
podman exec -d step-superset-app superset mcp run --host 0.0.0.0 --port 5008
```

长期使用建议做成 compose 常驻服务（command 覆盖为 mcp run），而非 `exec -d`。

### 3.2 客户端接入

OpenCode（`opencode.json`）：

```json
{
  "mcp": {
    "superset": {
      "type": "remote",
      "url": "http://localhost:5008/mcp",
      "enabled": true
    }
  }
}
```

Claude Desktop（配置文件位于 `%APPDATA%\Claude\claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "superset": {
      "url": "localhost:5008/mcp"
    }
  }
}
```

也可裸 HTTP 调用（JSON-RPC，`tools/list` 无需握手即可列工具）。

### 3.3 实测行为与坑

| 行为 | 实测结果 |
|---|---|
| 服务启动 | `superset mcp run` 后 StreamableHTTP session manager 就绪，`/mcp` 端点响应 |
| `tools/list` 未带凭据 | 正常返回工具清单 |
| `tools/call` 未带凭据 | **报错 `Authentication required. No valid credentials provided`**——`MCP_DEV_USERNAME` 是工具执行的必要配置，漏配表现为所有工具不可用 |
| 端点路径 | `http://<host>:<port>/mcp`（不是根路径） |

同名陷阱：网络上另有 `superset.sh`（agent 工作平台，MCP 端点 `api.superset.sh/mcp`），
与 Apache Superset 无关，配置时勿混用。

## 4. 公网上线要求

官方 `PRODUCTION.md` 自认的缺口：HTTP 明文默认、无速率限制、无 metrics 导出、日志为
debug 级。公网部署必须在部署层补齐，以下按红线到加固排序。

### 4.1 认证：JWT（红线）

`MCP_DEV_USERNAME` 模式 = 任何能触达 5008 端口的请求**都以同一用户全权操作**。
公网必须删除 dev 配置并启用 JWT：

```python
# superset_config.py
MCP_AUTH_ENABLED = True
MCP_JWT_ALGORITHM = "RS256"   # 推荐 RS256 + JWKS 对接 OIDC；HS256 用共享密钥
MCP_JWKS_URI = "https://<身份提供方>/.well-known/jwks.json"
MCP_JWT_ISSUER = "https://<身份提供方>/"
MCP_JWT_AUDIENCE = "superset-mcp"
MCP_REQUIRED_SCOPES = ["mcp:read"]   # 需要写操作的客户端才另发 mcp:write
# HS256 时密钥走环境变量，严禁写进仓库：
# MCP_JWT_SECRET = os.environ.get("MCP_JWT_SECRET")
```

JWT 校验规则（官方文档口径）：`exp` 强制校验；`iss`/`aud` 建议配置；`sub` 解析为
Superset 用户（该用户必须存在）。RS256 静态公钥方案可替代 JWKS。特殊场景可用
`MCP_AUTH_FACTORY` 注入自定义认证工厂，优先级高于所有内置 JWT 配置。

### 4.2 授权：专用低权限账号（红线）

JWT 只解决「谁在调用」，权限由 `sub` 解析到的 Superset 用户决定——**为 MCP 建专用
账号，绝不用 admin**：

| 原则 | 做法 |
|---|---|
| 最小 scope | 只读场景只发 `mcp:read`；建图/存查询的客户端才发 `mcp:write` |
| 数据面 | 该账号仅加入必要的 dataset/dashboard 权限集，RLS 自动生效 |
| 连接面 | 不授予数据库连接管理权限，防止经 MCP 新建连接绕过既有安全体系（如本仓库 DuckDB 三层闸门的白名单模型） |
| 可归因 | 专用账号让元库 `logs` 表中每条 MCP 调用可归因，与人类用户区分 |

### 4.3 传输与网络收口（红线）

MCP server 默认 HTTP 明文（官方文档原话要求生产必须启用 HTTPS）。收口模型：

```text
公网 → 反向代理(TLS 终止 + HSTS) ─┬─ /mcp/*     → 127.0.0.1:5008
                                  └─ /superset/* → 127.0.0.1:8088
```

- mcp 进程绑 `127.0.0.1`（不是 `0.0.0.0`），仅反代可达；容器端口 5008 不映射宿主机
- 反代校验 `Host`/`Origin` 头（MCP 规范要求，防 DNS rebinding），只转发合法域名
- 客户端到反代全程 HTTPS，反代到 mcp 进程走本机回环

### 4.4 限流与并发

- MCP 服务自身无速率限制（官方自认）——限流放在反代层（如 nginx `limit_req`），
  按 IP 或 Authorization 维度
- `MCP_TOOL_WORKERS` 设定进程内仓库类调用的并发池；**池满直接返回 busy 而非排队**
  （官方设计：防慢查询拖垮整个 MCP 进程），按数据源查询时长合理设置

### 4.5 监控与审计（官方缺口，自行补齐）

- 反代 access log 单独留存：谁在何时调了哪个工具
- 定期审计元库 `logs` 表中 MCP 专用账号的行为，对 `generate_dashboard`/
  `update_chart` 等 mutate 工具设置调用告警阈值
- 官方无 metrics 导出——需要 Prometheus 监控时自行在反代或旁路补

### 4.6 版本要求

- **RC 版本不上公网**：7.0.0rc3 用于内网验证；公网等 7.0 正式版 tag 发布后升级镜像
- 持续跟踪 Superset 官方安全公告；MCP 服务随主版本一起升级，无独立补丁通道

## 5. 上线前检查清单

| # | 检查项 | 红线 |
|---|---|---|
| 1 | `MCP_DEV_USERNAME` 已删除，`MCP_AUTH_ENABLED = True` + JWT/JWKS 生效 | 是 |
| 2 | MCP 专用低权限账号 + 只读 scope，未复用 admin | 是 |
| 3 | mcp 进程绑回环地址，5008 无公网/宿主机映射 | 是 |
| 4 | 反代 TLS + HSTS + Host/Origin 校验 + 限流 | 是 |
| 5 | 镜像为正式版（非 RC） | 是 |
| 6 | `MCP_TOOL_WORKERS` 已按查询时长设置 | 建议 |
| 7 | mutate 工具调用告警与 `logs` 审计就位 | 建议 |
| 8 | JWT 密钥经环境变量注入，未出现在任何文件 | 是 |

## 关联文档

- [DuckDB 文件访问控制](./duckdb.md)：三层闸门序列与 S3 白名单，MCP 进程同样生效
- [MaxCompute 接入](./maxcompute.md)：`execute_sql` 经 odps 连接的分区裁剪与参数口径
- [故障排查](./troubleshooting.md)：含 7.0 API/响应形态已知问题
