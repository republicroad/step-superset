# step-superset

**Connect Superset 7.0 to everything** —— MaxCompute · DuckDB · DuckLake · Quack
的一站式部署与安全接入实战栈，附 PostgreSQL / MySQL 等标准数据库连接模板。

一套可复现的 compose 部署、派生镜像、三层闸门安全配置与端到端验收脚本：
四条实测打通的数据源链路，外加标准数据库的模板化接入。

![Superset](https://img.shields.io/badge/Superset-7.0.0rc3-20A6C5?logo=apache)
![DuckDB](https://img.shields.io/badge/DuckDB-2.0.0-FFF100?logo=duckdb)
![pyodps](https://img.shields.io/badge/pyodps-0.13.3-orange)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)
![Runtime](https://img.shields.io/badge/podman%20%7C%20docker-6.2%20%7C%20any-8940FA)

> 🔎 全部关键结论本机实测（2026-11-19/20）· 文档中文 · 面向个人 / 内网部署与开源参考

## ✨ 数据源一览

| 数据源 | 通道 | 亮点 |
|---|---|---|
| **MaxCompute** | pyodps 方言 `odps://` | 查询加速（MaxQA）+ 对象名缓存，一键固化连接 |
| **DuckDB** | `duckdb://` + duckdb-engine | 本地湖文件直读，三层闸门安全（白名单 → 关总闸 → 上锁） |
| **DuckLake** | parquet 视图 / quack 中转 | catalog 语义与轻量查询两条路径 |
| **Quack** | DuckDB 2.0 官方远程协议 | `quack_query` 表函数直查远程 DuckDB，零初始化钩子依赖 |
| **PostgreSQL** | 标准 URI（`database_conn` 模板） | 模板 + 通用创建脚本一键接入 |
| **MySQL** | 标准 URI（`database_conn` 模板） | 模板 + 通用创建脚本一键接入 |

## 🚀 快速开始

```bash
# 0) 克隆后进入仓库根目录
cp .env.example .env        # 填入 MaxCompute AK 等数据源凭据（永不提交）

# 1) 拉取官方镜像（国内镜像源三级回退）
./scripts/pull-image.sh 7.0.0rc3

# 2) 构建派生镜像（官方镜像 + pyodps + duckdb-engine + 方言补丁）
podman build --http-proxy=false -t localhost/superset-mc:7.0.0rc3 .

# 3) 启动全栈（init 自动跑迁移 + 建 admin 账号；.env 自动读取）
podman compose up -d                    # 或 docker compose up -d

# 4) 重建 DuckDB 演示数据（main.duckdb + parquet + DuckLake catalog）
podman exec -i step-superset-app python - < scripts/demo/prepare-ducklake-demo.py

# 5) 验证
podman exec -i step-superset-app python - < scripts/verify/verify-duckdb.py
podman exec -i step-superset-app python - < scripts/verify/verify-maxcompute.py
```

浏览器访问 `http://127.0.0.1:8088`，账号 `admin`，密码见 compose
（示例值 `admin-demo-2026`，生产必改）。

## 🏗 架构

```text
                    ┌────────────────────────────────────┐
                    │        Superset 7.0 (7.0.0rc3)     │
                    │  派生镜像 = 官方 + pyodps +        │
                    │      duckdb-engine + 方言补丁      │
                    └───┬──────────┬───────────┬────────┘
          odps:// 方言  │ duckdb://│ ops.demo  │ quack_query('quack:host', ...)
          ┌────────────┘│          │ 视图      │
          ▼             │          ▼           ▼
┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐ ┌─────────────────────┐
│  MaxCompute      │ │ /data/duckdb │ │ quack server     │ │ 标准 SQLAlchemy 源  │
│  (查询加速)      │ │ main.duckdb  │ │ (可 ATTACH       │ │ PostgreSQL / MySQL  │
└──────────────────┘ │ + parquet    │ │  DuckLake)       │ │ doris / starrocks   │
                     └──────────────┘ └──────────────────┘ │ trino（模板接入）   │
                                                           └─────────────────────┘
```

## 📁 目录结构

```text
docker-compose.yml      全栈编排（坑注释内建）
Dockerfile              派生镜像（ARG SUPERSET_TAG 版本可切换）
docker/                 superset_config.py（中文/Jinja 开关）+ engine_patches.py（DuckDB 闸门 + odps 方言补丁）
patch/enable_duckdb.py  duckdb 方言黑名单补丁（仅测试/内网环境）
.env.example            数据源凭据模板（真实凭据放 .env，永不提交）
database_conn/          数据源连接模板（pg/mysql/duckdb/maxcompute/doris/starrocks/trino）
scripts/                pull-image.sh（拉镜像）；verify/（三数据源 + MCP 端到端验收）；setup/（连接创建与数据集固化）；demo/（DuckLake 演示数据、Quack 客户端、MCP 建图组盘闭环演示）
docs/                   四数据源 + MCP + troubleshooting 详述（中文，内容自包含）
```

## 🔒 安全边界

- 本仓库**不包含任何 id / key / 内网地址**：数据源凭据全部走 `.env`
  （已被 .gitignore 排除），模板与示例仅含占位符
- compose 内 Superset 自身密码为 `change-me` 风格示例值，生产部署必须覆盖
  （`.env` 设同名变量即可，compose 自动读取）
- duckdb 方言黑名单补丁仅限**测试/内网环境**；生产环境不要解除，
  并按 [docs/duckdb.md](docs/duckdb.md) 三层闸门收敛实际可读范围

## 📖 数据源文档

| 文档 | 内容 |
|---|---|
| [docs/maxcompute.md](docs/maxcompute.md) | 连接串参数、查询加速、全表扫描保护（`max_pt()`）、AK 最小权限 |
| [docs/duckdb.md](docs/duckdb.md) | 三层闸门最终序列（逐行注释）、四条铁律、S3/OSS 配方、区域与 DNS 坑 |
| [docs/ducklake.md](docs/ducklake.md) | DuckLake 两条落地路径与 ATTACH 持久性实证 |
| [docs/quack.md](docs/quack.md) | Quack 远程协议三种用法与实测边界 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 镜像构建三坑、7.0 API 口径、官方已知事项 |

## ⚠️ 已知边界

- 基线为 7.0.0rc3（投票期版本），正式版发布后改 Dockerfile ARG 重建即可
- 7.0 中文翻译覆盖不全（AI 回填未审校），设置类页面部分菜单为英文
- Doris / StarRocks / Trino 模板的驱动未预装，使用前按
  `database_conn/README.md` 追加 pip 包并重建镜像
- 真实密码的存放位置与覆盖方式：见 [docs/troubleshooting.md](docs/troubleshooting.md)
  与 `.env.example` 内注释（compose 只自动读 `.env`，不读其他名字）

## 📄 License

待定——发布前请补充 LICENSE 文件（如 Apache-2.0 / MIT）；
当前仓库内容在 LICENSE 确定前请勿直接再分发。
