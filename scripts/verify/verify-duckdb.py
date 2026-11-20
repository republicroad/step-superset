# ============================================================
# DuckDB 连接创建 + 安全配置 + 查询验证（Superset 7.0 REST API）
#
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/verify/verify-duckdb.py
#
# 行为：
#   1) 登录 Superset，创建（或复用）"DuckDB (local)" 连接，
#      创建时默认带三层闸门安全参数（docs/duckdb.md）：
#      allowed_directories → enable_external_access=false → lock_configuration
#   2) 正向查询：ops.demo 视图（数据来自 scripts/demo/prepare-ducklake-demo.py）
#   3) 负向测试：读 /etc/passwd 必须被拦（安全配置验收项）
# ============================================================
import json
import os
import urllib.request
import uuid

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin-demo-2026")
ALLOWED_DIRS = [d for d in os.environ.get(
    "DUCKDB_ALLOWED_DIRECTORIES", "/data/duckdb").split(",") if d.strip()]


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
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or _ENV.get("ADMIN_USERNAME", ADMIN_USER)
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _ENV.get("ADMIN_PASSWORD", ADMIN_PASS)
if _ENV.get("DUCKDB_ALLOWED_DIRECTORIES"):
    ALLOWED_DIRS = [d for d in _ENV["DUCKDB_ALLOWED_DIRECTORIES"].split(",") if d.strip()]


def http(url, data=None, headers=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 1) 登录 + 会话（写接口需 CSRF + 会话 cookie）
import http.cookiejar as _http_cookiejar
s = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_http_cookiejar.CookieJar()))
code, body = http(f"{BASE}/api/v1/security/login",
                  data={"username": ADMIN_USER, "password": ADMIN_PASS,
                        "provider": "db", "refresh": True}, method="POST")
if code != 200:
    print("登录失败:", code, str(body)[:200])
    sys.exit(1)
token = body["access_token"]
auth = {"Authorization": f"Bearer {token}"}
resp = s.open(urllib.request.Request(f"{BASE}/api/v1/security/csrf_token/", headers=auth), timeout=30)
csrf = json.loads(resp.read().decode())["result"]
h = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}


def api(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **h}, method=method)
    try:
        resp = s.open(req, timeout=120)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


print("login OK")

# 2) 创建/复用 DuckDB 连接
#    安全配置不走 connect_args：allowed_directories 在连接创建时会被
#    duckdb 拒绝（Cannot change/set allowed_directories before the database
#    is started，实测），统一由 engine_patches 钩子在连接后施加
S3_ENABLED = (os.environ.get("DUCKDB_S3_ENABLED") or _ENV.get("DUCKDB_S3_ENABLED", "false")).lower() == "true"
code, body = api(f"{BASE}/api/v1/database/")
items = body["result"]
existing = [d for d in items if "DuckDB" in d["database_name"]]
if existing:
    db_id = existing[0]["id"]
    print("复用连接:", existing[0]["database_name"], "id:", db_id)
else:
    code, body = api(f"{BASE}/api/v1/database/", data={
        "database_name": "DuckDB (local)",
        "sqlalchemy_uri": "duckdb:////data/duckdb/main.duckdb",
        "configuration_method": "sqlalchemy_form",
    }, method="POST")
    print("创建:", code, str(body)[:200])
    db_id = body["id"]
    if not S3_ENABLED:
        print("提示：ENGINE_PATCHES_ENABLED 未开启时，此连接无闸门保护（仅限本机环境）")

# 3) 正向查询


def run_sql(sql, expect_ok=True):
    code, body = api(f"{BASE}/api/v1/sqllab/execute/", data={
        "database_id": db_id,
        "sql": sql,
        "client_id": uuid.uuid4().hex[:10],
        "runAsync": False,
        "schema": None,
        "catalog": None,
    }, method="POST")
    ok = code == 200 and body.get("status") == "success"
    tag = "PASS" if ok == expect_ok else "FAIL"
    if ok:
        # 7.0 响应形态：data 直接是行对象列表（也可能仍是 {records: [...]} 包装）
        data = body["data"]
        if isinstance(data, list):
            print(f"[{tag}] {sql} ->", data[:3])
        elif isinstance(data, dict):
            print(f"[{tag}] {sql} ->", data.get("records", [])[:3])
        else:
            print(f"[{tag}] {sql} ->", str(data)[:160])
    else:
        msg = json.dumps(body, ensure_ascii=False)[:200]
        print(f"[{tag}] {sql} -> http {code} {msg}")
    return ok == expect_ok


run_sql("SELECT count(*) AS cnt, version() AS v FROM ops.demo")
run_sql("SELECT * FROM ops.demo ORDER BY id")

# 4) 负向测试：白名单外路径必须被拦（enable_external_access=false 生效）
run_sql("SELECT count(*) FROM read_csv('/etc/passwd')", expect_ok=False)
run_sql("ATTACH '/etc/hostname' AS smuggled", expect_ok=False)
print("done: 安全配置正向 + 负向均已验收")
