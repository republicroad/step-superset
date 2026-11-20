# ============================================================
# 从备份 JSON 恢复 SQL Lab 保存查询（幂等：同名跳过）
#
# 前置：备份 JSON 已放入容器 /tmp/backup_saved_queries.json
#   （宿主机：podman cp backup/saved_queries_20261120.json step-superset-app:/tmp/）
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/restore-saved-queries-from-backup.py
#
# 逻辑：
#   1) 读取备份 JSON（GET /saved_query/{id} 的 result 形态）
#   2) 建立 旧 db_id → 新 db_id 映射（按 backend/名称解析当前连接）
#   3) 同名（label）已存在 → 跳过；否则 POST 创建
# ============================================================
import json
import os
import sys
import urllib.request

BASE = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
BACKUP_JSON = os.environ.get("BACKUP_JSON", "/tmp/backup_saved_queries.json")


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
if not ADMIN_PASS:
    print("缺少 ADMIN_PASSWORD（.env 或环境变量）")
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
        resp = opener.open(req, timeout=180)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400]}


# 2) 读备份 JSON
backup = json.load(open(BACKUP_JSON, encoding="utf-8"))
if isinstance(backup, dict):  # 兼容 {result: [...]} 包装
    backup = backup.get("result", [])
print("备份保存查询:", len(backup), "条")

# 3) 当前连接映射（按备份条目的 SQL 特征推断归属）
code, body = api(f"{BASE}/api/v1/database/")
cur = body["result"]
duckdb_id = next((d["id"] for d in cur if d.get("backend") == "duckdb"), None)
# odps 主连接：backend=odps 且名称不含 ebike（不依赖名字关键字，改名也兼容）
odps_dbs = [d for d in cur if d.get("backend") == "odps"]
odps_main_id = next((d["id"] for d in odps_dbs if "ebike" not in (d["database_name"] or "").lower()),
                    odps_dbs[0]["id"] if odps_dbs else None)
odps_ebike_id = next((d["id"] for d in odps_dbs if "ebike" in (d["database_name"] or "").lower()), None)
print("当前连接: duckdb =", duckdb_id, "| odps =", odps_main_id, "| odps-ebike =", odps_ebike_id)


def map_db_by_sql(sql):
    s = (sql or "").lower()
    if "duckdb" in s or "read_json" in s or "read_parquet" in s or "duckdb_settings" in s or "duckdb_extensions" in s:
        return duckdb_id
    if "ebike" in s:
        return odps_ebike_id
    return odps_main_id  # 默认主数仓连接


# 4) 已有保存查询（规范化 SQL 去重用）
code, body = api(f"{BASE}/api/v1/saved_query/")
sqs = body["result"] if isinstance(body.get("result"), list) else []
existing_norm = {" ".join((q.get("sql") or "").split()): q.get("label") for q in sqs}

created, skipped_dupe, skipped_map = 0, 0, 0
for item in backup:
    label = item.get("label", "")
    sql = item.get("sql", "")
    new_db = map_db_by_sql(sql)
    if not sql or new_db is None:
        print(f"跳过（无法映射数据库）: {label}")
        skipped_map += 1
        continue
    norm = " ".join(sql.split())
    if norm in existing_norm:
        print(f"跳过（SQL 已存在）: {label} -> {existing_norm[norm]}")
        skipped_dupe += 1
        continue
    code, body = api(f"{BASE}/api/v1/saved_query/", data={
        "label": label,
        "description": (item.get("description") or "") + "（自 2026-11-20 备份恢复）",
        "sql": sql,
        "db_id": new_db,
        "schema": item.get("schema"),
    }, method="POST")
    if code in (200, 201):
        print(f"恢复: {label} -> id {body.get('id')} (db {new_db})")
        created += 1
        existing_norm[norm] = label
    else:
        print(f"失败: {label} http {code} {str(body)[:150]}")

print(f"=== 完成 === 恢复 {created}，SQL 重复跳过 {skipped_dupe}，无法映射 {skipped_map}")
