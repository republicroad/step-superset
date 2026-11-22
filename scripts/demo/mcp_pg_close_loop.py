# ============================================================
# MCP 闭环演示：PG 数据源 → 虚拟数据集 → 图表 → 仪表盘 → 验证 → 清理
#
# 用法（仓库根目录）：
#   podman cp scripts/demo/mcp_pg_close_loop.py step-superset-app:/tmp/
#   podman exec step-superset-app python /tmp/mcp_pg_close_loop.py
#
# 数据源：栈内 PostgreSQL 容器（superset_db，即元数据库本身）——
#   连接串直接复用容器环境变量 SUPERSET__SQLALCHEMY_DATABASE_URI，
#   演示数据取元库 logs 表（按 action 聚合，天然有真实数据）。
#
# 链路（全部经 MCP call_tool，除建/删连接外）：
#   execute_sql 冒烟 → create_virtual_dataset → generate_chart(xy bar)
#   → get_chart_data → generate_dashboard → list_dashboards 验证
#   → 清理（delete_dashboard / delete_chart / REST 删数据集）
#   PG 连接保留供复用（MCP 无连接写工具，经 REST 建/删）。
# ============================================================
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8088"
MCP_URL = "http://step-superset-mcp:5008/mcp"
DB_NAME = "PG (metadata demo)"
STAMP = time.strftime("%H%M%S")
DATASET_NAME = f"PG demo · MCP 操作日志分布 {STAMP}"
CHART_TITLE = f"MCP 演示 · logs 操作类型分布 {STAMP}"
DASH_TITLE = f"MCP 闭环演示仪表盘 {STAMP}"
SLUG = f"mcp-close-loop-demo-{STAMP}"
DATASET_SQL = ("SELECT action, count(*) AS cnt FROM logs "
               "GROUP BY action ORDER BY cnt DESC")


def _load_env_file():
    env = {}
    for path in ("/app/.env", os.path.join(os.getcwd(), ".env")):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
            if env:
                break
    return env


_ENV = _load_env_file()
ADMIN_USER = os.environ.get("ADMIN_USERNAME") or _ENV.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD") or _ENV.get("ADMIN_PASSWORD", "admin-demo-2026")

failures = 0


def tag(ok, msg):
    global failures
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
    if not ok:
        failures += 1


# ---------- Superset REST（建/删连接与数据集，MCP 无此写工具） ----------
import http.cookiejar

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


AUTH = {}


def rest(url, data=None, method="GET"):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data else None,
        headers={"Content-Type": "application/json", **AUTH}, method=method)
    try:
        resp = opener.open(req, timeout=60)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}


code, body = rest(f"{BASE}/api/v1/security/login",
                  data={"username": ADMIN_USER, "password": ADMIN_PASS,
                        "provider": "db", "refresh": True}, method="POST")
if code != 200:
    print("LOGIN FAIL:", code, str(body)[:200])
    sys.exit(1)
token = body["access_token"]
resp = opener.open(urllib.request.Request(f"{BASE}/api/v1/security/csrf_token/",
                   headers={"Authorization": f"Bearer {token}"}), timeout=30)
csrf = json.loads(resp.read().decode())["result"]
AUTH = {"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf, "Referer": BASE}
print("login OK")

uri = os.environ.get("SUPERSET__SQLALCHEMY_DATABASE_URI", "")
code, body = rest(f"{BASE}/api/v1/database/?q=(page_size:100)")
items = body.get("result") or []
pg_rows = [d for d in items if d.get("database_name") == DB_NAME]
if pg_rows:
    pg_id = pg_rows[0]["id"]
    print(f"复用连接 {DB_NAME} id={pg_id}")
else:
    code, body = rest(f"{BASE}/api/v1/database/", data={
        "database_name": DB_NAME, "sqlalchemy_uri": uri,
        "configuration_method": "sqlalchemy_form"}, method="POST")
    pg_id = body.get("id")
    tag(code == 201 and pg_id, f"REST 建 PG 连接 id={pg_id}")

# ---------- MCP 客户端 ----------
def parse(body, ct=""):
    body = (body or "").strip()
    if not body:
        return None
    if "text/event-stream" in ct:
        lines = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        return json.loads(lines[-1])
    return json.loads(body)


def post(payload):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    r = urllib.request.urlopen(req, timeout=120)
    return parse(r.read().decode(), r.headers.get("Content-Type", ""))


post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                 "clientInfo": {"name": "close-loop", "version": "1"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"})
next_id = 10


def call_tool(name, request_args):
    global next_id
    resp = post({"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                 "params": {"name": "call_tool",
                            "arguments": {"name": name, "arguments": {"request": request_args}}}})
    next_id += 1
    return resp


def text_of(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except Exception:
        return json.dumps(resp)[:400]


def tool_error(resp):
    if resp is None:
        return "no response"
    if "error" in resp:
        return str(resp["error"])[:200]
    if resp.get("result", {}).get("isError"):
        return text_of(resp)[:200]
    return None


# 1) execute_sql 冒烟
r = call_tool("execute_sql", {"database_id": pg_id,
                              "sql": "SELECT action, count(*) AS cnt FROM logs "
                                     "GROUP BY action ORDER BY cnt DESC LIMIT 8"})
err = tool_error(r)
body_txt = text_of(r)
ok = (not err) and '"success":true' in body_txt
tag(ok, f"execute_sql 冒烟（logs 按 action 聚合）{'' if ok else '-> ' + (err or body_txt)[:160]}")
if ok:
    rows = json.loads(body_txt).get("rows", [])
    print(f"       数据样例: {rows[:3]}")

# 2) create_virtual_dataset
r = call_tool("create_virtual_dataset", {
    "database_id": pg_id, "sql": DATASET_SQL, "dataset_name": DATASET_NAME,
    "description": "MCP 闭环演示：元库 logs 按 action 聚合"})
err = tool_error(r)
body_txt = text_of(r)
dataset_id = None
try:
    payload = json.loads(body_txt)
    dataset_id = payload.get("dataset_id") or payload.get("id") or \
        (payload.get("dataset") or {}).get("id")
except Exception:
    pass
tag((not err) and dataset_id is not None,
    f"create_virtual_dataset -> dataset_id={dataset_id} {'' if not err else '-> ' + err}")

# 3) generate_chart（xy 柱状图；save_chart=true 才落库，否则只返回预览态）
r = call_tool("generate_chart", {
    "dataset_id": dataset_id,
    "chart_name": CHART_TITLE,
    "save_chart": True,
    "config": {"chart_type": "xy", "kind": "bar",
               "x": {"name": "action", "label": "操作类型"},
               "y": [{"name": "cnt", "aggregate": "SUM", "label": "调用次数"}],
               "row_limit": 10}})
err = tool_error(r)
body_txt = text_of(r)
chart_id = None
try:
    payload = json.loads(body_txt)
    chart_id = payload.get("chart_id") or payload.get("id") or \
        (payload.get("chart") or {}).get("id")
except Exception:
    pass
tag((not err) and chart_id is not None,
    f"generate_chart(xy bar) -> chart_id={chart_id} {'' if not err else '-> ' + err}")
if err is None:
    print(f"       返回样例: {body_txt[:200]}")

# 4) get_chart_data
if chart_id is not None:
    r = call_tool("get_chart_data", {"identifier": chart_id})
    err = tool_error(r)
    body_txt = text_of(r)
    ok = (not err) and ("rows" in body_txt or "data" in body_txt)
    tag(ok, f"get_chart_data {'' if ok else '-> ' + (err or body_txt)[:160]}")
    if ok:
        try:
            rows = json.loads(body_txt).get("rows", [])
            print(f"       图表取数样例: {rows[:3]}")
        except Exception:
            pass

# 5) generate_dashboard
r = call_tool("generate_dashboard", {
    "chart_ids": [chart_id], "dashboard_title": DASH_TITLE,
    "description": "MCP 闭环演示：PG 元库日志分布", "slug": SLUG})
err = tool_error(r)
body_txt = text_of(r)
dash_id = None
try:
    payload = json.loads(body_txt)
    dash_id = payload.get("dashboard_id") or payload.get("id") or \
        (payload.get("dashboard") or {}).get("id")
except Exception:
    pass
tag((not err) and dash_id is not None,
    f"generate_dashboard -> dashboard_id={dash_id} slug={SLUG} {'' if not err else '-> ' + err}")

# 6) list_dashboards 验证
r = call_tool("list_dashboards", {})
err = tool_error(r)
body_txt = text_of(r)
found = False
try:
    for d in json.loads(body_txt).get("dashboards", []):
        if d.get("id") == dash_id or d.get("slug") == SLUG:
            found = True
            break
except Exception:
    pass
tag((not err) and found,
    f"list_dashboards 验证新盘可见 {'(slug=' + SLUG + ')' if found else ''} "
    f"{'' if not (err or not found) else '-> ' + (err or body_txt or 'not found')[:160]}")

# ---------- 清理（图表/仪表盘走 MCP，数据集走 REST） ----------
if dash_id is not None:
    r = call_tool("delete_dashboard", {"identifier": dash_id})
    tag(tool_error(r) is None, f"清理 delete_dashboard id={dash_id} "
        f"{'' if tool_error(r) is None else '-> ' + tool_error(r)}")
if chart_id is not None:
    r = call_tool("delete_chart", {"identifier": chart_id})
    tag(tool_error(r) is None, f"清理 delete_chart id={chart_id} "
        f"{'' if tool_error(r) is None else '-> ' + tool_error(r)}")
if dataset_id is not None:
    code, body = rest(f"{BASE}/api/v1/dataset/{dataset_id}", method="DELETE")
    tag(code == 200, f"清理 REST 删数据集 id={dataset_id} (http {code})")

print(f"\nPG 连接保留复用: id={pg_id} ({DB_NAME})")
print("done:", "闭环全部通过" if failures == 0 else f"{failures} 项失败")
sys.exit(1 if failures else 0)
