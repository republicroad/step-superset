# Superset 全局配置：默认简体中文
# 官方约定：PYTHONPATH 里的 superset_config.py 会被 superset/config.py 自动导入，
# compose 已把 ./docker 目录挂载到 /app/pythonpath
BABEL_DEFAULT_LOCALE = "zh"

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

_hook_on = _env_local.get("DUCKDB_SECURITY_HOOK", "true").lower() == "true" \
    or _env_local.get("DUCKDB_S3_ENABLED", "false").lower() == "true"
if _hook_on:
    import duckdb_engine_security  # noqa: E402  与本文件同在 /app/pythonpath

    duckdb_engine_security.apply(_env_local)
