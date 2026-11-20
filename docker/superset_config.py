# Superset 全局配置：默认简体中文 + 东八区
# 官方约定：PYTHONPATH 里的 superset_config.py 会被 superset/config.py 自动导入，
# compose 已把 ./docker 目录挂载到 /app/pythonpath
BABEL_DEFAULT_LOCALE = "zh"

# Celery beat / Alerts & Reports 的 cron 调度时区（须与容器 TZ 一致）
CELERY_TIMEZONE = "Asia/Shanghai"

# SQL/数据集层 Jinja 模板渲染（filter_values 等宏）——MaxCompute 分区表
# 「默认最新分区 + 筛选器可覆盖 ds」模式的前提。
# 注意：必须写进 FEATURE_FLAGS 字典。官方 config.py 的 FEATURE_FLAGS 在
# import 时即固化各 flag 值，单独写顶层 ENABLE_TEMPLATE_PROCESSING=True
# 只改顶层键、不影响字典，实际判定仍为关闭（实测踩坑）
FEATURE_FLAGS = {"ENABLE_TEMPLATE_PROCESSING": True}

LANGUAGES = {
    "en": {"flag": "us", "name": "English"},
    "zh": {"flag": "cn", "name": "简体中文"},
}

# ------------------------------------------------------------
# DuckDB 安全钩子（推荐开启，默认 true）
#
# 每个连接建立时自动执行三层闸门序列（白名单 → 关总闸 → 上锁），
# 两种 profile 由 .env 内容自动决定：
#   配置了 DUCKDB_S3_* → S3/OSS profile（含 httpfs/secret/CA）
#   仅 DUCKDB_ALLOWED_DIRECTORIES → 本地白名单 profile
# 不开启时 Superset 行为与官方镜像完全一致（无任何闸门保护）。
# 顺序与单向性见 docs/duckdb.md。
# ------------------------------------------------------------
import os as _os

_env_local = {}
for _p in ("/app/.env", _os.path.join(_os.getcwd(), ".env")):
    if _os.path.isfile(_p):
        with open(_p, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _env_local[_k.strip()] = _v.strip()
        break

# 引擎补丁总开关（原 DUCKDB_SECURITY_HOOK 改名，旧键名保留兼容读取）
_patches_on = _env_local.get("ENGINE_PATCHES_ENABLED",
                             _env_local.get("DUCKDB_SECURITY_HOOK", "true")).lower() == "true" \
    or _env_local.get("DUCKDB_S3_ENABLED", "false").lower() == "true"
if _patches_on:
    import engine_patches  # noqa: E402  与本文件同在 /app/pythonpath

    engine_patches.apply(_env_local)
