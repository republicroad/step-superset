# ============================================================
# DuckDB 三层闸门 + S3 安全钩子 链路验收（四用例）
#
# 前置：
#   1) .env 已填 ADMIN_PASSWORD
#   2) 已运行 scripts/demo/prepare-ducklake-demo.py 重建演示数据
#   3) （S3 用例）.env 已启用 DUCKDB_S3_ENABLED=true + DUCKDB_S3_*
#      凭据可为假值——验收口径是「假凭证请求真实打到存储端，
#      收到 403/404 类 HTTP 错误」即证明闸门/TLS/凭证链路全通
#
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/verify/verify-duckdb-chain.py
# ============================================================
import json
import os
import sys
import uuid

import requests

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")


def _load_env_file():
    """读 .env（兼容 .env.local 与当前目录变体），环境变量优先于文件"""
    env = {}
    for path in ("/app/.env", "/app/.env.local",
                 os.path.join(os.getcwd(), ".env"), os.path.join(os.getcwd(), ".env.local")):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
    return env


_ENV = _load_env_file()
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or _ENV.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _ENV.get("ADMIN_PASSWORD", "")
S3_PREFIX = (os.environ.get("DUCKDB_S3_BUCKET_PREFIX") or _ENV.get("DUCKDB_S3_BUCKET_PREFIX", "s3://fake-bucket-abc/")).rstrip("/") + "/"

# 凭据真伪决定用例 2 的预期：假凭据 → 必须收到存储端 HTTP 错误（链路贯通证明）；
# 真实凭据 → 用例 2 应当真实读取成功（SQL 从 DUCKDB_S3_TEST_SQL 传入）
FAKE_CREDS = (os.environ.get("DUCKDB_S3_KEY_ID") or _ENV.get("DUCKDB_S3_KEY_ID", "")).startswith("fake-")
S3_TEST_SQL = os.environ.get("DUCKDB_S3_TEST_SQL") or _ENV.get("DUCKDB_S3_TEST_SQL", "")

# 会话（7.0 口径：CSRF 需 JWT + 会话 cookie）
r = requests.post(f"{BASE}/api/v1/security/login",
                  json={"username": ADMIN_USER, "password": ADMIN_PASS,
                        "provider": "db", "refresh": True}, timeout=30)
r.raise_for_status()
token = r.json()["access_token"]
s = requests.Session()
csrf = s.get(f"{BASE}/api/v1/security/csrf_token/",
             headers={"Authorization": f"Bearer {token}"}, timeout=30).json()["result"]
h = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}

dbs = s.get(f"{BASE}/api/v1/database/", headers={"Authorization": f"Bearer {token}"},
            params={"q": "(page_size:100)"}, timeout=30).json()["result"]
duck = [d for d in dbs if "DuckDB" in d["database_name"]]
if not duck:
    print("未找到 DuckDB 连接——先运行 scripts/verify/verify-duckdb.py 创建")
    sys.exit(1)
db_id = duck[0]["id"]
print("连接:", duck[0]["database_name"], "id:", db_id)


def run(sql):
    r = s.post(f"{BASE}/api/v1/sqllab/execute/", headers=h, json={
        "database_id": db_id, "sql": sql, "client_id": uuid.uuid4().hex[:10],
        "runAsync": False, "schema": None, "catalog": None,
    }, timeout=120)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if isinstance(body, list):  # 7.0 错误响应偶发为 list
        return False, json.dumps(body, ensure_ascii=False)[:200]
    if r.status_code == 200 and body.get("status") == "success":
        # 7.0 响应形态：data 直接是行对象列表（也可能仍是 {records: [...]} 包装）
        data = body["data"]
        if isinstance(data, list):
            return True, str(data[:3])[:160]
        if isinstance(data, dict):
            return True, str(data.get("records", [])[:3])[:160]
        return True, str(data)[:160]
    errs = body.get("errors", [])
    return False, (errs[0].get("message", "") if errs else str(body)[:200])


def http_marker(detail):
    """判定 S3 错误是否为「已到达存储端」类（链路贯通）"""
    return "HTTP" in detail or "InvalidAccessKeyId" in detail or "403" in detail or "404" in detail


out_prefix = S3_PREFIX + "x.parquet"
other_prefix = S3_PREFIX.replace("fake-bucket-abc", "other-bucket") if "fake-bucket-abc" in S3_PREFIX \
    else S3_PREFIX.rsplit("/", 2)[0] + "-out-of-scope/"

# 凭据真伪决定用例 2 的预期：假凭据 → 必须收到存储端 HTTP 错误（链路贯通证明）；
# 真实凭据 → 用例 2 应当真实读取成功（SQL 从 DUCKDB_S3_TEST_SQL 传入）
FAKE_CREDS = (os.environ.get("DUCKDB_S3_KEY_ID") or _ENV.get("DUCKDB_S3_KEY_ID", "")).startswith("fake-")
S3_TEST_SQL = os.environ.get("DUCKDB_S3_TEST_SQL") or _ENV.get("DUCKDB_S3_TEST_SQL", "")

cases = [
    ("1 本地白名单内", "SELECT count(*) AS cnt FROM ops.demo", True, None),
]
if FAKE_CREDS:
    cases.append(("2 S3 白名单前缀（假凭证/假桶）",
                  f"SELECT count(*) FROM read_parquet('{out_prefix}')", False, http_marker))
elif S3_TEST_SQL:
    cases.append(("2 S3 白名单前缀读取（真实凭据）", S3_TEST_SQL, True, None))
else:
    print("（跳过用例 2：真实凭据但未提供 DUCKDB_S3_TEST_SQL）")
cases += [
    ("3 S3 前缀外对象",
     f"SELECT count(*) FROM read_parquet('{other_prefix}x.parquet')", False,
     "file system operations are disabled"),
    ("4 本地任意文件", "SELECT count(*) FROM read_csv('/etc/passwd')", False,
     "file system operations are disabled"),
]

fails = 0
for label, sql, expect_ok, expect_err in cases:
    ok, detail = run(sql)
    if callable(expect_err):
        passed = (ok == expect_ok) and expect_err(detail)
    else:
        passed = (ok == expect_ok) and (expect_err is None or expect_err in detail)
    status = "PASS" if passed else "FAIL"
    if not passed:
        fails += 1
    print(f"[{status}] {label}: {detail[:160]}")

print("=== 结果 ===", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(0 if fails == 0 else 1)
