# Superset 连接 MaxCompute

> 覆盖：连接方式、连接串参数、UI 步骤、AK 准备、查询坑与验证方法。
> 结论：**现行唯一官方通道是 pyodps SQLAlchemy 方言（连接串 `odps://`）**，推荐参数
> `interactive_mode=true&reuse_odps=true&cache_names=true`；推荐组合在本仓库镜像内已实测通过。
> 版本锚定：Superset 7.0 + pyodps 0.13.3 + SQLAlchemy 2.0.52，行为实测于 2026-11-19。

## 1. 连接方式

MaxCompute 官方 BI 兼容总览将 Superset 指向「基于 PyODPS 使用 SQLAlchemy 接入」方案：
pyodps 从 0.10.0 起内置 SQLAlchemy 方言，通过 entry point 注册 `odps` / `maxcompute`
两个方言名；0.13.3 起还自带 Superset 引擎规范注册（`superset.db_engine_specs` 入口点），
安装后 Superset 自动识别为 `backend: odps`（本仓库实测）。

连接串格式：

```text
odps://<AK_ID>:<AK_SECRET>@<PROJECT>/?endpoint=<ENDPOINT>&interactive_mode=true&reuse_odps=true&cache_names=true&cache_seconds=1200
```

## 2. 连接串参数

| 参数 | 作用 | 说明 |
|---|---|---|
| `interactive_mode=true` | 查询加速总开关 | 加速失败自动回退普通离线 SQL，回退作业按普通 SQL 计费 |
| `reuse_odps=true` | 强制复用连接 | 官方明确点名对 Superset 这类工具有性能收益 |
| `fallback_policy=...` | 回退策略 | 可选；`unsupported,upgrading,noresource` / `timeout` / `all`，逗号分隔 |
| `cache_names=true&cache_seconds=N` | 对象名缓存 | pyodps ≥ 0.12.0；Superset 频繁列举表对象，不开会有明显 UI 延迟 |

参数依据：MaxCompute 官方文档「查询加速」章节方式五（官方文档核实 2026-11）。

## 3. UI 配置步骤

1. 设置 → 数据库连接 → + 数据库 → Other / SQLAlchemy URI
2. 粘贴连接串（占位符替换为真实值）
3. Advanced → Other 勾选「在 SQL Lab 中展示」；保存后数据源识别为 `backend: odps`

Endpoint 是 MaxCompute 服务的接入点，按项目地域选择（本地测试用公网地址，
生产尽量走 VPC Endpoint）；项目名是 MaxCompute 项目，不是 DataWorks 工作空间名。

## 4. AK 准备

- 使用 RAM 子账号 AK，禁用主账号 AK；只授予目标项目 `Select` / `Download`
- Superset 里该连接对所有可见用户生效，AK 等价于「数仓只读通行证」，按此设计权限模型
- AK 含 `/ + =` 等特殊字符时必须 URL 编码后再填入连接串
- 凭据只保存在 Superset 数据库连接配置中（元数据库内），本仓库不落地任何 AK

## 5. 查询坑：全表扫描保护（最高频）

项目开启全表扫描保护后，查分区表必须带分区谓词，否则报
`ODPS-0130071: ... full scan with all partitions`。标准写法：

```sql
SELECT * FROM <table> WHERE <分区列> = max_pt('<table>');
```

在 Superset 里把带 `max_pt()` 过滤的 SQL 做成**虚拟数据集**，图表查询就不会撞保护。
分区列名因表而异（常见 `ds` / `dt`），可用 `DESC <table>` 确认。

## 6. 日期字段现状

社区 2022 年曾记录 pyodps 方言的日期字段问题（时间列不被识别等）。
本仓库链路（Superset 7.0 + SQLAlchemy 2.0.52 + pyodps 0.13.3）实测：
DATETIME 字段正常返回与序列化，未复现历史问题。若在新版本组合上遇到日期异常，
先在 SQL Lab 用 `SELECT <日期列> LIMIT 5` 对照数据库原样输出定位层次。

## 7. 验证方法

```bash
# 1) 在 UI 配好 odps:// 数据源后，仓库根目录 cp .env.example .env
#    填入 SUPERSET_ADMIN_PASSWORD
# 2) 运行（冒烟查询默认 select 1，可用环境变量 VERIFY_SQL 覆盖）
VERIFY_SQL="select 1" podman exec -i step-superset-app python - < scripts/verify-maxcompute.py
```

脚本走 Superset 查询管道全链路（do_connect + 结果获取），成功即证明连接、方言、
结果通道全部正常。7.0 的 REST API 口径差异已内置（见 troubleshooting.md）。
