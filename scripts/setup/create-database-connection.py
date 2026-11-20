# ============================================================
# 通用数据源连接创建：读模板 → 填占位符 → 幂等创建 → 冒烟
#
# 环境变量：
#   CONNECTION_TEMPLATE    模板路径（相对 /app/database_conn），必填
#                          如 database_conn/maxcompute.json
#   CONNECTION_OVERRIDES   JSON 对象，填充模板里的自由占位符
#                          如 {"name":"生产库","DB_HOST":"10.x.x.x"}
#   SUPERSET_BASE_URL / ADMIN_USERNAME / ADMIN_PASSWORD  同其他脚本
#
# 占位符解析顺序：CONNECTION_OVERRIDES > /app/.env > os.environ
# 特殊处理：odps:// 连接串的用户/密码段自动 URL 编码（AK 含 / + = 时无需手工转码）
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/setup/create-database-connection.py
# ============================================================
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
TEMPLATE = os.environ.get("CONNECTION_TEMPLATE", "")
OVERRIDES = json.loads(os.environ.get("CONNECTION_OVERRIDES", "{}"))
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or os.environ.get("SUPERSET_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or os.environ.get("SUPERSET_ADMIN_PASSWORD", "")

if not TEMPLATE:
    print("缺少 CONNECTION_TEMPLATE（如 database_conn/maxcompute.json）")
    sys.exit(1)


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


ENV_FILE = _load_env_file()


def resolve(key: str) -> str | None:
    """占位符取值顺序：OVERRIDES > 容器环境变量 > .env 文件"""
    return OVERRIDES.get(key) or os.environ.get(key) or ENV_FILE.get(key)


def http(url, data=None, headers=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 1) 读模板
tpath = TEMPLATE if os.path.isfile(TEMPLATE) else os.path.join("/app", TEMPLATE)
if not os.path.isfile(tpath):
    print("模板不存在:", tpath)
    sys.exit(1)
template_text = open(tpath, encoding="utf-8").read()
print("模板:", tpath)

# 2) 找出全部占位符并解析
placeholders = sorted(set(re.findall(r"<([A-Za-z_][A-Za-z0-9_]*)>", template_text)))
values = {}
missing = []
for key in placeholders:
    v = resolve(key)
    if v is None:
        missing.append(key)
    else:
        values[key] = v
if missing:
    print("未填充的占位符:", ", ".join(f"<{k}>" for k in missing),
          "——用 CONNECTION_OVERRIDES 或 .env 提供")
    sys.exit(1)
for k, v in values.items():
    print(f"  <{k}> = {'***' if any(s in k.upper() for s in ('PASSWORD', 'SECRET')) else v}")

# 3) 替换；odps:// 的用户/密码段自动 URL 编码（AK 常含 / + =）
payload_text = template_text
for k, v in values.items():
    payload_text = payload_text.replace(f"<{k}>", v)

payload = json.loads(payload_text)
uri = payload.get("sqlalchemy_uri", "")
if uri.startswith("odps://"):
    head, _, tail = uri.partition("@")
    userinfo = head.partition("://")[2]
    if ":" in userinfo and "%" not in userinfo:  # 未编码过才编码
        user, _, pw = userinfo.rpartition(":")
        userinfo = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}"
    payload["sqlalchemy_uri"] = f"odps://{userinfo}@{tail}"

# 4) 登录 + CSRF（7.0 口径）
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
        resp = opener.open(req, timeout=180)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 5) 幂等创建（同名复用）
code, body = api(f"{BASE}/api/v1/database/")
name = payload["database_name"]
same = [d for d in body["result"] if d["database_name"] == name]
if same:
    db_id = same[0]["id"]
    print("复用连接:", name, "id:", db_id)
else:
    code, body = api(f"{BASE}/api/v1/database/", data=payload, method="POST")
    print("创建:", code, str(body)[:200])
    if code not in (200, 201):
        sys.exit(1)
    db_id = body["id"]

# 6) 冒烟 select 1
code, body = api(f"{BASE}/api/v1/sqllab/execute/", data={
    "database_id": db_id, "sql": "select 1",
    "client_id": uuid.uuid4().hex[:10], "runAsync": False,
    "schema": None, "catalog": None,
}, method="POST")
if code == 200 and body.get("status") == "success":
    print("冒烟 select 1: PASS")
else:
    errs = body.get("errors", [])
    print("冒烟失败:", (errs[0].get("message", "") if errs else str(body))[:300])
