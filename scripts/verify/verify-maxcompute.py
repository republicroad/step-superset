# ============================================================
# MaxCompute 连接端到端验证（Superset 7.0 REST API）
#
# 前置：Superset 已启动，且已在 UI 配好 odps:// 数据源
# 用法（仓库根目录）：
#   1) cp .env.example .env 填入 ADMIN_PASSWORD 等
#   2) podman exec -i step-superset-app python - < scripts/verify/verify-maxcompute.py
#
# 验证内容：登录 → 定位 odps 数据源 → 跑 --sql 对应查询（默认 select 1）
# 7.0 API 口径已内置：csrf_token 端点要求 JWT；/sqllab/execute/ 载荷
# 字段收紧（database_label / tmp_chart_name 已删除）
# ============================================================
import json
import os
import sys
import urllib.request
import uuid

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")


def load_env_local():
    """读 .env：compose 已把仓库根的 .env 挂到容器 /app/.env"""
    for path in ("/app/.env", os.path.join(os.getcwd(), ".env")):
        if os.path.isfile(path):
            env = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
            return env
    return {}


ENV = load_env_local()
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or ENV.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or ENV.get("ADMIN_PASSWORD", "")
if not ADMIN_PASS:
    print("缺少 ADMIN_PASSWORD（.env 或环境变量）")
    sys.exit(1)

SQL = None
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if args:
    SQL = args[0]
elif len(sys.argv) > 2:
    SQL = sys.argv[2]
if not SQL:
    SQL = os.environ.get("VERIFY_SQL", "select 1")


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


# 1) 登录（7.0：login 无需 CSRF；csrf_token 端点反而要求 JWT）
code, body = http(f"{BASE}/api/v1/security/login",
                  data={"username": ADMIN_USER, "password": ADMIN_PASS,
                        "provider": "db", "refresh": True}, method="POST")
if code != 200:
    print("登录失败:", code, str(body)[:200])
    sys.exit(1)
token = body["access_token"]
auth = {"Authorization": f"Bearer {token}"}

# 2) CSRF（写接口必需，且要带着同一会话 cookie）
req = urllib.request.Request(f"{BASE}/api/v1/security/csrf_token/", headers=auth)
import http.cookiejar
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
resp = opener.open(urllib.request.Request(f"{BASE}/api/v1/security/csrf_token/", headers=auth), timeout=30)
csrf = json.loads(resp.read().decode())["result"]
h = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}


def api(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **h}, method=method)
    try:
        resp = opener.open(req, timeout=120)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 3) 定位 odps 数据源
code, body = api(f"{BASE}/api/v1/database/")
items = body["result"]
odps_dbs = [d for d in items if d.get("backend") == "odps"]
if not odps_dbs:
    print("未找到 odps 数据源——请先在 UI 配置 odps:// 连接")
    sys.exit(1)
db_id = odps_dbs[0]["id"]
print("数据源:", odps_dbs[0]["database_name"], "id:", db_id)

# 4) 执行查询（走 Superset 查询管道：do_connect + 结果获取全链路）
code, body = api(f"{BASE}/api/v1/sqllab/execute/", data={
    "database_id": db_id,
    "sql": SQL,
    "client_id": uuid.uuid4().hex[:10],
    "runAsync": False,
    "schema": None,
    "catalog": None,
}, method="POST")
print("http", code)
if code == 200 and body.get("status") == "success":
    # 7.0 响应形态：data 直接是行对象列表（也可能仍是 {columns/records} 包装）
    data = body["data"]
    if isinstance(data, list):
        print("rows:", data[:5])
    elif isinstance(data, dict):
        print("columns:", [c["name"] for c in data.get("columns", [])])
        for row in data.get("records", [])[:5]:
            print("row:", row)
    else:
        print("data:", str(data)[:200])
else:
    print(json.dumps(body, ensure_ascii=False)[:600])
