# ============================================================
# duckdb_engine 安全钩子：连接建立后按固定顺序执行三层闸门
#
# 背景（docs/duckdb.md 第 4/6 节）：三层闸门（白名单 → 关总闸 → 上锁）
# 全部是实例级 SET，且顺序敏感、基本单向；allowed_directories/allowed_paths
# 在连接创建时（connect config）设置会被拒——
#   Invalid Input Error: Cannot change/set allowed_directories
#   before the database is started
# 因此闸门必须在连接建立后施加，而 duckdb-engine 没有 post-connect
# 初始化钩子——本补丁 monkeypatch 方言的 connect() 补上这个钩子。
#
# 两种 profile（由 env 决定，接线见 docker/superset_config.py）：
#   S3 profile（DUCKDB_S3_ENABLED=true 且配置了 S3 前缀）：
#     LOAD httpfs → 关持久 secret → CREATE SECRET(内存) → ca_cert_file
#     → allowed_paths(CA) → allowed_directories(S3 前缀 + 本地目录)
#     → 关总闸 → 关扩展自动通道 → lock_configuration
#   local profile（无 S3 前缀，仅 DUCKDB_ALLOWED_DIRECTORIES）：
#     allowed_directories → 关总闸 → 关扩展自动通道 → lock
#
# 验收口径：白名单内读取成功；名单外对象与本地任意文件一律
#   Permission Error: file system operations are disabled
# ============================================================
import logging
import os

log = logging.getLogger(__name__)


def _sql_literal(value: str) -> str:
    """SQL 字符串字面量转义（单引号加倍）"""
    return "'" + value.replace("'", "''") + "'"


def _list_literal(values) -> str:
    return "[" + ", ".join(_sql_literal(v) for v in values if v) + "]"


def _read_env_file(paths):
    for path in paths:
        if os.path.isfile(path):
            env = {}
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
            return env
    return {}


def _apply_security(conn, env: dict) -> None:
    ca_file = env.get("DUCKDB_CA_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    s3_prefixes = [p.strip() for p in env.get("DUCKDB_S3_BUCKET_PREFIX", "").split(",") if p.strip()]
    local_dirs = [p.strip() for p in env.get("DUCKDB_ALLOWED_DIRECTORIES", "").split(",") if p.strip()]
    if not s3_prefixes and not local_dirs:
        raise ValueError("DUCKDB_S3_BUCKET_PREFIX 与 DUCKDB_ALLOWED_DIRECTORIES 均未配置，白名单为空")

    allowed = s3_prefixes + local_dirs
    seq = ["SET extension_directories = ['/opt/duckdb-extensions']"]

    if s3_prefixes:
        # S3 分支：httpfs 必须在关闸前加载（关闸后 LOAD 被拦）
        # 可选参数（URL_STYLE / USE_SSL）：env 未设时省略，用 httpfs 默认值（vhost / true）
        secret_parts = [
            "TYPE s3",
            f"KEY_ID {_sql_literal(env.get('DUCKDB_S3_KEY_ID', ''))}",
            f"SECRET {_sql_literal(env.get('DUCKDB_S3_SECRET', ''))}",
        ]
        if env.get("DUCKDB_S3_ENDPOINT"):
            secret_parts.append(f"ENDPOINT {_sql_literal(env['DUCKDB_S3_ENDPOINT'])}")
        if env.get("DUCKDB_S3_REGION"):
            secret_parts.append(f"REGION {_sql_literal(env['DUCKDB_S3_REGION'])}")
        if env.get("DUCKDB_S3_URL_STYLE"):
            secret_parts.append(f"URL_STYLE {_sql_literal(env['DUCKDB_S3_URL_STYLE'])}")
        if env.get("DUCKDB_S3_USE_SSL"):
            secret_parts.append(f"USE_SSL {_sql_literal(env['DUCKDB_S3_USE_SSL'])}")

        seq += [
            "LOAD httpfs",
            "SET allow_persistent_secrets = false",  # 凭证只留内存，不落盘
            "CREATE SECRET s3_security (" + ", ".join(secret_parts) + ")",
            f"SET ca_cert_file = {_sql_literal(ca_file)}",  # httpfs 选项，须在 LOAD 后设置
            f"SET allowed_paths = {_list_literal([ca_file])}",
        ]

    # 共同收尾：白名单 → 关总闸 → 关扩展自动通道 → 上锁
    seq += [
        f"SET allowed_directories = {_list_literal(allowed)}",
        "SET enable_external_access = false",
        "SET autoload_known_extensions = false",
        "SET autoinstall_known_extensions = false",
        "SET allow_community_extensions = false",
        "SET lock_configuration = true",
    ]
    for sql in seq:
        conn.execute(sql)
    log.info("duckdb security sequence applied: %d whitelist entries", len(allowed))


def apply(env: dict) -> None:
    """monkeypatch duckdb_engine 的 Dialect.connect；必须在引擎创建前调用"""
    import duckdb_engine

    original_connect = duckdb_engine.Dialect.connect

    def secure_connect(self, *args, **kwargs):
        conn = original_connect(self, *args, **kwargs)
        _apply_security(conn, env)  # 失败即抛出，宁可连接失败也不静默放开
        return conn

    duckdb_engine.Dialect.connect = secure_connect
    log.info("duckdb_engine Dialect.connect patched (security hook active)")
