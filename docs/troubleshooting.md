# Troubleshooting：镜像构建与 Superset 7.0 验证

> 覆盖：派生镜像构建三坑、元数据库静默回退、7.0 REST API 口径差异、黑名单补丁边界、
> 两个官方已知事项。
> 范围限定：只覆盖本仓库的镜像与 Superset 自身问题；容器引擎/机器层运维不在本仓库范围。
> 版本锚定：Superset 7.0.0rc2 / pyodps 0.13.3 / duckdb 2.0.0，全部实测于 2026-11-19/20。

## 1. 镜像构建三坑（pyodps / duckdb-engine 装不上或装错）

| 现象 | 原因 | 解法 |
|---|---|---|
| `pip install` 成功但运行时 `import odps` / `import duckdb` 失败 | 运行时解释器是 `/app/.venv/bin/python`，PATH 上的 pip 指向系统 Python | `pip --python /app/.venv/bin/python install ...` |
| `ensurepip` 引导失败 | venv 为 uv 布局，未内置 pip | 跳过 ensurepip，直接用系统 pip 的 `--python` 参数 |
| 写 site-packages 权限拒绝 | 镜像默认 `USER superset`（uid 1000） | 构建段 `USER root`，安装后切回 |

补充：`pip install pyodps` 的导入名是 `odps`（`import pyodps` 会失败）；
构建产物 tag 带 `localhost/` 前缀，compose 引用必须写全。

## 2. 元数据库静默回退 SQLite

现象：compose 配了 `DATABASE_*` 系列变量，但元库进了 SQLite
（判据：`SELECT count(*) FROM information_schema.tables` 只有几表或直接无库）。

原因：Superset 5.x/6.x/7.0 镜像内**没有任何代码消费 `DATABASE_DIALECT` 等旧变量**
（实测 grep 引导脚本与 config.py），正确入口是 Flask 环境变量覆盖约定：

```yaml
SUPERSET__SQLALCHEMY_DATABASE_URI: postgresql+psycopg2://superset:<密码>@superset_db:5432/superset
```

验证：连元数据库应看到 50+ 张表。

## 3. 7.0 REST API 口径差异（自动化脚本必读）

| 变更 | 旧口径 | 7.0 口径 |
|---|---|---|
| CSRF token | 登录前可匿名取 | `/security/csrf_token/` **要求先带 JWT**（实测 401） |
| CSRF 校验 | 仅需 header | 需要同一会话的 cookie + `X-CSRF-Token`（裸请求报 session token missing） |
| `/sqllab/execute/` 载荷 | 允许 `database_label` / `tmp_chart_name` | 两字段已删除，传入报 `Unknown field`（422） |
| `client_id` | 可重复使用 | 唯一约束，重复报 UniqueViolation（500），必须每次随机 |
| 成功响应 `data` | `{columns: [...], records: [...]}` 包装 | **直接是行对象列表** `[{...}, ...]`，列定义在响应顶层 `columns`（实测两形态都会出现，解析需兼容） |

`scripts/verify-maxcompute.py` / `verify-duckdb.py` 已按 7.0 口径实现，可直接抄。

## 4. duckdb 方言黑名单补丁的边界

- 补丁（`patch/enable_duckdb.py`）只在**镜像构建时**生效，改 patch 后需重建镜像
- 解除黑名单 = 接受「能访问 Superset 的用户可读容器内文件系统」的前提，
  必须配合 `docs/duckdb.md` 三层闸门把实际可读范围收敛到白名单
- 生产/多租户环境不要解除；Superset 官方默认拦截是有意设计

## 5. 官方已知事项（7.0rc2 时点）

- **中文翻译覆盖不全**：简中为 AI 辅助回填（官方 changelog 标注 needs review），
  设置类页面部分菜单保持英文，属预期行为，无全局开关可强制补全
- **加密字段引擎升级提示**：启动日志警告 `SQLALCHEMY_ENCRYPTED_FIELD_ENGINE`
  为 legacy `aes`（AES-CBC 无认证），建议按官方 runbook 迁移到 `aes-gcm`：
  备份元数据库 → `superset re-encrypt-secrets --engine aes-gcm` → 改配置 → 重启 → 再跑一次清扫
- **缓存与限流默认内存实现**：`CACHE_TYPE` NullCache、flask-limiter 内存存储的
  UserWarning 属默认行为，测试栈可忽略；生产接 Redis 后消失

## 6. 快速定位流程

```bash
# 1) 应用是否活着
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8088/health

# 2) 应用日志（启动阶段报错在这里）
podman logs step-superset-app --tail 50

# 3) 元数据库确认
podman exec step-superset-app python -c "print('ok')"
```

启动慢（1 分钟以上）属正常：init 容器先跑 Alembic 迁移，app 等 init 完成才启动。
