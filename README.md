# step-superset · Superset 7.0 数据源实战栈

Superset 7.0 连接四类数据源的最佳实践与可复现部署：**MaxCompute**（pyodps 方言）、
**DuckDB**（本地文件/内存）、**DuckLake**（parquet 视图 / quack 中转）、
**Quack**（DuckDB 2.0 官方远程协议）。

> 版本锚定：Superset 7.0.0rc2（`ARG SUPERSET_TAG` 可切换正式版）+ pyodps 0.13.3 +
> duckdb-engine 0.17.0 + duckdb 2.0.0 | 全部关键结论本机实测，实测日期 2026-11-19/20

## 快速开始

```bash
# 0) 克隆后进入仓库根目录
cp .env.example .env        # 填入 MaxCompute AK 等数据源凭据（永不提交）

# 1) 拉取官方镜像（国内镜像源三级回退）
./scripts/pull-image.sh 7.0.0rc2

# 2) 构建派生镜像（官方镜像 + pyodps + duckdb-engine + 方言补丁）
podman build --http-proxy=false -t localhost/superset-mc:7.0.0rc2 .

# 3) 启动全栈（init 自动跑迁移 + 建 admin 账号）
#    .env 会被 compose 自动读取，覆盖 compose 内的示例密码
podman compose up -d                    # 或 docker compose up -d

# 4) 重建 DuckDB 演示数据（main.duckdb + parquet + DuckLake catalog）
podman exec -i step-superset-app python - < scripts/prepare-ducklake-demo.py

# 5) 验证
podman exec -i step-superset-app python - < scripts/verify-duckdb.py     # DuckDB 正向+负向
podman exec -i step-superset-app python - < scripts/verify-maxcompute.py # 需先在 UI 配 odps:// 数据源
```

浏览器访问 `http://127.0.0.1:8088`，账号 `admin`，密码见 compose（示例值 `admin-demo-2026`，
生产必改）。

## 数据源矩阵

| 数据源 | 通道 | 镜像依赖 | 验证脚本 | 文档 |
|---|---|---|---|---|
| MaxCompute | pyodps 方言 `odps://` URI | pyodps（已装） | `verify-maxcompute.py` | [docs/maxcompute.md](docs/maxcompute.md) |
| DuckDB | duckdb-engine `duckdb://` URI | duckdb-engine（已装）+ 方言补丁 | `verify-duckdb.py` | [docs/duckdb.md](docs/duckdb.md) |
| DuckLake | parquet 视图（默认）/ quack server 中转 | 复用 DuckDB 链路 | `prepare-ducklake-demo.py` + `verify-duckdb.py` | [docs/ducklake.md](docs/ducklake.md) |
| Quack 远程 | `quack_query` 表函数（虚拟数据集 SQL） | duckdb 2.0 内置扩展 | `quack-server.py` + `quack_client.py` | [docs/quack.md](docs/quack.md) |

## 架构

```text
                    ┌────────────────────────────────────┐
                    │        Superset 7.0 (7.0.0rc2)     │
                    │  派生镜像 = 官方 + pyodps +        │
                    │             duckdb-engine + 补丁   │
                    └───┬──────────┬───────────┬────────┘
          odps:// 方言  │ duckdb://│ ops.demo  │ quack_query('quack:host', ...)
                        │          │ 视图      │
                        ▼          ▼           ▼
              ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
              │  MaxCompute  │ │ /data/duckdb │ │ quack server     │
              │  (查询加速)  │ │ main.duckdb  │ │ (可 ATTACH       │
              └──────────────┘ │ + parquet    │ │  DuckLake)       │
                               └──────────────┘ └──────────────────┘
```

## 目录结构

```text
docker-compose.yml      全栈编排（坑注释内建）
Dockerfile              派生镜像（ARG SUPERSET_TAG 版本可切换）
docker/superset_config.py   中文等全局配置（挂载到 /app/pythonpath）
patch/enable_duckdb.py  duckdb 方言黑名单补丁（仅测试/内网环境）
.env.example      数据源凭据模板（真实凭据放 .env，永不提交）
scripts/                拉镜像 / 重建演示数据 / 连接创建 / 各数据源验证脚本
database_conn/          数据源连接模板（pg/mysql/duckdb/maxcompute/doris/starrocks/trino）
docs/                   四数据源 + troubleshooting 详述（零外链）
```

## 安全边界（开放仓库声明）

- 本仓库**不包含任何 id / key / 内网地址**；MaxCompute AK 等凭据全部走
  `.env`（已被 .gitignore 排除），示例文件仅含空值占位
- compose 内 Superset 自身密码为 `change-me` 风格示例值，生产部署必须覆盖
  （`.env` 中设同名变量即可，compose 会自动读取）
- duckdb 方言黑名单补丁仅限**测试/内网环境**；生产环境不要解除，
  并按 [docs/duckdb.md](docs/duckdb.md) 三层闸门收敛实际可读范围
- 脚本中的 table / project 等均为占位符，使用时替换为自己的资源

## 已知边界

- 基线为 7.0.0rc2（投票期版本），正式版发布后改 Dockerfile ARG 重建即可
- 7.0 中文翻译覆盖不全（AI 回填未审校），设置类页面部分菜单为英文
- `scripts/` 里的验证脚本通过容器 stdin 执行（`podman exec -i ... python -`），
  脚本自身不进容器
