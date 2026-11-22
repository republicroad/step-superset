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

# ------------------------------------------------------------
# MCP 服务（docs/mcp.md；compose 内 superset_mcp 常驻服务）
#
# 两种模式互斥配置，均由 .env 注入，键值不入仓库：
#   开发模式（仅本机）：MCP_DEV_MODE=true 且 MCP_DEV_USERNAME 存在时，
#     未认证的工具调用一律以该用户身份执行；该用户必须已存在于 Superset。
#     公网部署必须关闭（红线），改用下方 JWT。
#   生产模式：MCP_AUTH_ENABLED=true，JWT/JWKS 校验，sub 解析为 Superset
#     用户，RBAC/RLS 随之逐用户生效；scope 经 MCP_REQUIRED_SCOPES 控制。
# 两者都未配置时 MCP 服务仍可启动，但所有 tools/call 被拒
# （Authentication required——MCP_DEV_USERNAME 漏配即此表现，实测）。
# ------------------------------------------------------------
if _env_local.get("MCP_DEV_MODE", "false").lower() == "true" and _env_local.get("MCP_DEV_USERNAME"):
    MCP_DEV_USERNAME = _env_local["MCP_DEV_USERNAME"]

if _env_local.get("MCP_AUTH_ENABLED", "false").lower() == "true":
    MCP_AUTH_ENABLED = True
    MCP_JWT_ALGORITHM = _env_local.get("MCP_JWT_ALGORITHM", "RS256")
    if _env_local.get("MCP_JWT_SECRET"):        # HS256 共享密钥，严禁写进仓库
        MCP_JWT_SECRET = _env_local["MCP_JWT_SECRET"]
    if _env_local.get("MCP_JWKS_URI"):          # RS256 走 JWKS 对接 OIDC
        MCP_JWKS_URI = _env_local["MCP_JWKS_URI"]
    if _env_local.get("MCP_JWT_ISSUER"):
        MCP_JWT_ISSUER = _env_local["MCP_JWT_ISSUER"]
    if _env_local.get("MCP_JWT_AUDIENCE"):
        MCP_JWT_AUDIENCE = _env_local["MCP_JWT_AUDIENCE"]
    MCP_REQUIRED_SCOPES = [s.strip() for s in
                           _env_local.get("MCP_REQUIRED_SCOPES", "mcp:read").split(",") if s.strip()]
