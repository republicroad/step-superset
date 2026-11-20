# database_conn · 数据源连接模板

四个 Superset 数据源连接模板（`POST /api/v1/database/` 的载荷格式）。
占位符 `<...>` 使用前替换；本目录**不含任何真实凭据**。

## 模板一览

| 文件 | 方言 | 说明 |
|---|---|---|
| [postgresql.json](postgresql.json) | `postgresql+psycopg2` | 密码含特殊字符需 URL 编码 |
| [mysql.json](mysql.json) | `mysql+pymysql` | 带 `charset=utf8mb4` |
| [duckdb.json](duckdb.json) | `duckdb` | 指向容器内演示主库；安全由三层闸门钩子自动施加（见 docs/duckdb.md），内存库形态改 `duckdb:///:memory:` |
| [maxcompute.json](maxcompute.json) | `odps` | AK 需 URL 编码；推荐直接用 `scripts/setup-maxcompute-connection.py`（自动处理编码与幂等） |
| [doris.json](doris.json) | `doris` | FE 查询端口默认 9030；库以 `internal.<库名>` 形式写在路径 |
| [starrocks.json](starrocks.json) | `starrocks` | 同 Doris（9030 / `internal.<库名>`） |
| [trino.json](trino.json) | `trino` | coordinator 默认 8080；启用密码认证时改为 `trino://<user>:<password>@host:8080/<catalog>` |

## 驱动前置（Doris / StarRocks / Trino）

这三个方言的 Python 驱动**未预装**，使用前在 Dockerfile 追加对应 pip 包并重建镜像：

| 方言 | pip 包 |
|---|---|
| Doris | `pydoris` |
| StarRocks | `starrocks` |
| Trino | `trino` |

缺驱动时连接创建可成功，但查询会报模块缺失（`ModuleNotFoundError`）。

## 使用方式

### 方式 A：脚本自动创建（推荐）

```bash
podman exec -i \
    -e CONNECTION_TEMPLATE=database_conn/maxcompute.json \
    -e CONNECTION_OVERRIDES={"name":"生产库"} \
    step-superset-app python - < scripts/create-database-connection.py
```

脚本行为：读模板 → 从 `/app/.env` 与 `CONNECTION_OVERRIDES` 填充 `<占位符>` →
`odps://` 连接串的用户/密码段自动 URL 编码 → 幂等创建（同名复用）→ `select 1` 冒烟。

### 方式 B：UI 手工

设置 → 数据库连接 → + 数据库 → Other → 把模板里的 `sqlalchemy_uri`
替换占位符后粘贴。AK 特殊字符（`/ + =`）同样要 URL 编码。

## 占位符约定

| 形式 | 来源 | 例子 |
|---|---|---|
| `<MC_*>` `<ADMIN_*>` | `.env` 同名键自动填充 | `<MC_AK_ID>`、`<MC_PROJECT>` |
| `<DB_*>` `<name>` 等其他 | `CONNECTION_OVERRIDES` 显式提供 | `{"name":"生产库","DB_HOST":"10.x.x.x"}` |

未填充的占位符会被脚本列出并拒绝创建（防止带占位符的连接入库）。
