# ============================================================
# 创建/复用 MaxCompute 数据源连接（幂等）
#
# 凭据来源：挂载的 /app/.env（MC_AK_ID / MC_AK_SECRET / MC_PROJECT / MC_ENDPOINT）
# 连接串参数见 docs/maxcompute.md
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/setup-maxcompute-connection.py
# ============================================================
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")


def _load_env_file():
    env = {}
    for path in ("/app/.env", "/app/.env.local"):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
    return env


ENV = _load_env_file()
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or ENV.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or ENV.get("ADMIN_PASSWORD", "")
AK_ID = os.environ.get("MC_AK_ID") or ENV.get("MC_AK_ID", "")
AK_SECRET = os.environ.get("MC_AK_SECRET") or ENV.get("MC_AK_SECRET", "")
PROJECT = os.environ.get("MC_PROJECT") or ENV.get("MC_PROJECT", "")
ENDPOINT = os.environ.get("MC_ENDPOINT") or ENV.get("MC_ENDPOINT", "")
DB_NAME = os.environ.get("MC_DB_NAME", "ODPS (MaxCompute)")

missing = [k for k, v in
           (("ADMIN_PASSWORD", ADMIN_PASS), ("MC_AK_ID", AK_ID),
            ("MC_AK_SECRET", AK_SECRET), ("MC_PROJECT", PROJECT), ("MC_ENDPOINT", ENDPOINT))
           if not v]
if missing:
    print("缺少配置:", ", ".join(missing), "——请检查 .env")
    sys.exit(1)


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


# 1) 登录 + CSRF（7.0 口径）
r = http(f"{BASE}/api/v1/security/login",
         data={"username": ADMIN_USER, "password": ADMIN_PASS,
               "provider": "db", "refresh": True}, method="POST")
if r[0] != 200:
    print("登录失败:", r[0], str(r[1])[:200])
    sys.exit(1)
token = r[1]["access_token"]
auth = {"Authorization": f"Bearer {token}"}

import http.cookiejar as _cjar
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cjar.CookieJar()))
csrf = json.loads(opener.open(urllib.request.Request(
    f"{BASE}/api/v1/security/csrf_token/", headers=auth), timeout=30).read().decode())["result"]
h = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}
print("login OK")


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


# 2) 构建 odps:// 连接串（AK 特殊字符 URL 编码）
def enc(v):
    return urllib.parse.quote(v, safe="")


uri = (f"odps://{enc(AK_ID)}:{enc(AK_SECRET)}@{PROJECT}/"
       f"?endpoint={ENDPOINT}&interactive_mode=true&reuse_odps=true"
       f"&cache_names=true&cache_seconds=1200")

# 3) 复用或创建
code, body = api(f"{BASE}/api/v1/database/")
items = body["result"]
odps = [d for d in items if d.get("backend") == "odps" or "ODPS" in d["database_name"]]
if odps:
    db_id = odps[0]["id"]
    print("复用连接:", odps[0]["database_name"], "id:", db_id)
else:
    code, body = api(f"{BASE}/api/v1/database/", data={
        "database_name": DB_NAME,
        "sqlalchemy_uri": uri,
        "configuration_method": "sqlalchemy_form",
    }, method="POST")
    print("创建:", code, str(body)[:200])
    if code not in (200, 201):
        sys.exit(1)
    db_id = body["id"]

# 4) 冒烟：select 1
code, body = api(f"{BASE}/api/v1/sqllab/execute/", data={
    "database_id": db_id,
    "sql": "select 1",
    "client_id": uuid.uuid4().hex[:10],
    "runAsync": False, "schema": None, "catalog": None,
}, method="POST")
if code == 200 and body.get("status") == "success":
    print("冒烟 select 1: PASS")
else:
    errs = body.get("errors", [])
    print("冒烟失败:", (errs[0].get("message", "") if errs else str(body))[:300])
