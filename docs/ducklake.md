# Superset 查询 DuckLake

> 覆盖：DuckLake 架构、ATTACH 不跨会话持久的实证、两条落地路径（parquet 视图 / quack server 中转）、
> 本仓库演示数据说明。
> 结论：**Superset 侧没有 DuckLake 的直接通路**（duckdb-engine 无 post-connect 初始化钩子，
> 无法在每次连接时重放 ATTACH；实测 ATTACH 也不跨会话持久），
> 生产推荐「专用 quack server 服务端 ATTACH 一次，Superset 走 quack 协议查询」；
> 轻量场景用 parquet 视图（本仓库默认，`ops.demo` 已可直接查）。
> 版本锚定：duckdb 2.0.0 + ducklake 扩展，行为实测于 2026-11-19。

## 1. DuckLake 是什么

DuckLake 是 DuckDB 官方的湖仓格式：**元数据存在 SQL catalog（SQLite/Postgres 等），
数据以 Parquet 落对象存储或本地盘**，提供 ACID 事务、快照、时间旅行等湖仓能力。
DuckDB 通过 `INSTALL ducklake; LOAD ducklake;` 加载扩展后 `ATTACH` 一个 catalog
即可像普通库一样读写。

## 2. 为什么 Superset 不能直接 ATTACH

两条实证证据（duckdb 2.0.0）：

1. **ATTACH 不跨会话持久**：对一个持久 main.duckdb 文件执行 `ATTACH 'ducklake:...' AS lake`
   并建表，关闭重开后 `duckdb_databases()` 里只剩 main/system/temp，挂载的 lake 消失
2. **duckdb-engine 没有 post-connect 初始化钩子**：其 connect_args 只支持
   `config` / `preload_extensions` / `register_filesystems`，任何 ATTACH 语句
   都无法在 Superset 建立连接时自动重放

因此「Superset 直连 DuckLake catalog」目前没有官方通路。

## 3. 落地路径 A：parquet 视图（本仓库默认，推荐起步）

`scripts/prepare-ducklake-demo.py` 产出的 `ops.demo` 视图就是这条路的样板：

```sql
CREATE VIEW ops.demo AS SELECT * FROM '/data/duckdb/data/*.parquet';
```

- 视图定义持久化在 main.duckdb，查询时现读文件
- 查询链路与 DuckLake 一致（列裁剪、谓词下推都作用于 parquet 文件）
- 没有 catalog / 事务 / 快照 / 时间旅行——轻量分析够用，强湖仓语义不要用这条路
- 对象存储场景：视图路径写 `s3://...`，前提见 docs/duckdb.md 第 6 节的扩展加载顺序问题

## 4. 落地路径 B：quack server 中转（生产推荐）

Quack 协议可以把一个 DuckDB 实例变成远程服务端（见 docs/quack.md），而
**服务端是长活进程，ATTACH DuckLake 一次即可常驻**：

```text
DuckLake(catalog + 对象存储)
        │ ATTACH（服务端进程内，一次配置常驻）
        ▼
quack server（专用容器，只挂数据卷与 catalog 配置）
        │ quack: 协议（HTTP，端口 9494）
        ▼
Superset 虚拟数据集 SQL：
SELECT * FROM quack_query('quack:<host>', 'SELECT ... FROM lake.ops.demo', token = '...');
```

好处：

- Superset 进程零文件/catalog 访问权限（安全面收敛在 server 侧）
- DuckLake 的全部湖仓语义（事务/快照/时间旅行）由服务端原生提供
- `quack_query` 是表函数，写在虚拟数据集 SQL 里即可，不依赖任何初始化钩子

服务端部署参考 `scripts/quack_server.py`（把 ATTACH ducklake 语句加在 quack_serve 之前即可）。

## 5. 本仓库演示物

| 文件 | 内容 | 用途 |
|---|---|---|
| `/data/duckdb/main.duckdb` | `ops.demo` 视图（指向 parquet） | Superset "DuckDB (local)" 连接直接查 |
| `/data/duckdb/data/part*.parquet` | 两个演示分区文件 | 路径 A 的数据 |
| `/data/duckdb/catalog.db` | DuckLake catalog（内含 `lake.ops.demo` 表） | DuckDB CLI 侧 ATTACH 流程演示 |

全部由 `scripts/prepare-ducklake-demo.py` 重建，git 不追踪二进制。

## 6. 边界与失效场景

- 路径 A 的视图在文件增删后视图定义不变（通配路径自动覆盖新文件），但**列结构变化**
  会导致查询报错，需要重建视图
- 路径 B 依赖 quack server 存活；server 容器重启后需要重放 ATTACH（放进启动脚本）
- catalog 用 SQLite 时并发写有限，多写方场景 catalog 换 Postgres（DuckLake 官方支持）
