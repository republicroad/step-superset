# ============================================================
# 固化埋点查询：虚拟数据集 + SQL Lab 保存查询
#
# 前置：.env 已填 ADMIN_PASSWORD；odps/duckdb 栈已启动；
#       DuckDB 连接已通过三层闸门验证（scripts/verify-duckdb-chain.py）
# 用法（仓库根目录）：
#   podman exec -i -e ADMIN_PASSWORD=<密码> step-superset-app python - \
#       < scripts/setup-track-events-dataset.py
# ============================================================
import json
import os
import sys
import urllib.request
import uuid

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASS:
    # 容器内从挂载的 /app/.env 读取
    for line in open("/app/.env", encoding="utf-8"):
        s = line.strip()
        if s.startswith("ADMIN_PASSWORD="):
            ADMIN_PASS = s.split("=", 1)[1]
if not ADMIN_PASS:
    print("缺少 ADMIN_PASSWORD")
    sys.exit(1)

DATASET_NAME = "track_events_raw"
SQL = ("SELECT * FROM read_json("
       "'s3://trackdata/track/event_version=3/year=2026/month=11/day=19/hour=10/*.log.gz')")
SAVED_LABEL = "埋点原始事件读取（event_version=3 / 2026-11-19 hour=10）"


def http(url, data=None, headers=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=300)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 1) 会话 + CSRF + 登录（7.0 口径）
r = http(f"{BASE}/api/v1/security/login",
         data={"username": ADMIN_USER, "password": ADMIN_PASS,
               "provider": "db", "refresh": True}, method="POST")
if r[0] != 200:
    print("登录失败:", r[0], str(r[1])[:200])
    sys.exit(1)
token = r[1]["access_token"]
auth = {"Authorization": f"Bearer {token}"}

import http.cookiejar
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf = json.loads(opener.open(urllib.request.Request(
    f"{BASE}/api/v1/security/csrf_token/", headers=auth), timeout=30).read().decode())["result"]
h = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}
print("login OK")


def api(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **h}, method=method)
    try:
        resp = opener.open(req, timeout=300)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 2) 定位 DuckDB 数据库
code, body = api(f"{BASE}/api/v1/database/")
duck = [d for d in body["result"] if "DuckDB" in d["database_name"]]
if not duck:
    print("未找到 DuckDB 连接")
    sys.exit(1)
db_id = duck[0]["id"]

# 3) 先执行一次查询拿列定义（7.0：data=行列表，columns 在响应顶层）
code, body = api(f"{BASE}/api/v1/sqllab/execute/", data={
    "database_id": db_id, "sql": SQL + " limit 10",
    "client_id": uuid.uuid4().hex[:10], "runAsync": False, "schema": None, "catalog": None,
}, method="POST")
if not (code == 200 and body.get("status") == "success"):
    print("查询失败，无法固化:", json.dumps(body, ensure_ascii=False)[:300])
    sys.exit(1)
src_cols = body.get("columns", [])
rows = body.get("data", [])
print(f"源查询 OK：{len(src_cols)} 列 / {len(rows)} 行样例")

dataset_columns = [{
    "column_name": c["name"],
    "type": c["type"],
    "is_dttm": c.get("type_generic") == 2 or "TIMESTAMP" in c["type"].upper(),
} for c in src_cols]

# 4) 创建/复用虚拟数据集（7.0：POST 只收 table_name/sql，columns/description 走 PUT）
code, body = api(f"{BASE}/api/v1/dataset/")
dsets = body["result"] if isinstance(body.get("result"), list) else []
existing = [d for d in dsets if d["table_name"] == DATASET_NAME]
if existing:
    ds_id = existing[0]["id"]
    print("复用数据集:", DATASET_NAME, "id:", ds_id)
else:
    code, body = api(f"{BASE}/api/v1/dataset/", data={
        "database": db_id,
        "table_name": DATASET_NAME,
        "sql": SQL,                      # 虚拟数据集：SQL 即定义；无 LIMIT，图表自行聚合
    }, method="POST")
    print("创建数据集:", code, str(body)[:200])
    if code not in (200, 201):
        sys.exit(1)
    ds_id = body["id"]

# 4b) 列定义与描述：7.0 在 POST 时服务端已自动拉取列定义；
#     仅当列为空时才用 PUT 补写（重复 PUT 会报 columns already exist）
code, body = api(f"{BASE}/api/v1/dataset/{ds_id}")
if code == 200:
    d = body["result"]
    print("数据集校验: columns =", len(d.get("columns", [])),
          "| sql 长度 =", len(d.get("sql", "")))
    if not d.get("columns"):
        put_columns = [{
            "column_name": c["column_name"],
            "type": c.get("type"),
            "is_dttm": c.get("is_dttm", False),
            "groupby": True,
            "filterable": True,
        } for c in dataset_columns]
        code, body = api(f"{BASE}/api/v1/dataset/{ds_id}", data={
            "columns": put_columns,
            "description": ("埋点原始事件（event_version=3）。当前范围：2026-11-19 hour=10 分区。"
                            "扩大范围：编辑数据集 SQL 里的 glob 路径即可"
                            "（如 day=19/* 或 event_version=3 全树）。"),
        }, method="PUT")
        print("补写列定义:", code, str(body)[:200])

# 6) 创建/复用 SQL Lab 保存查询
code, body = api(f"{BASE}/api/v1/saved_query/")
sqs = body["result"] if isinstance(body.get("result"), list) else []
existing_sq = [q for q in sqs if q.get("label") == SAVED_LABEL]
if existing_sq:
    print("复用保存查询: id", existing_sq[0]["id"])
else:
    code, body = api(f"{BASE}/api/v1/saved_query/", data={
        "label": SAVED_LABEL,
        "description": "原始埋点读取样例（read_json，event_version=3，2026-11-19 hour=10）",
        "sql": SQL + " limit 10",
        "db_id": db_id,
        "schema": None,
    }, method="POST")
    print("创建保存查询:", code, str(body)[:200])
    if code not in (200, 201):
        sys.exit(1)

print("=== 完成 ===")
print(f"虚拟数据集 id={ds_id}  SQL Lab 保存查询 label={SAVED_LABEL!r}")
