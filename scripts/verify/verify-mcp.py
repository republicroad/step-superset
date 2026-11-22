# ============================================================
# MCP 服务端到端验收（Superset 7.0 MCP / FastMCP StreamableHTTP）
#
# 用法（仓库根目录）：
#   podman exec -i step-superset-app python - < scripts/verify/verify-mcp.py
#   （宿主机直跑：MCP_URL=http://127.0.0.1:5008 python - < scripts/verify/verify-mcp.py）
#
# 实测口径（7.0.0rc3 + fastmcp 3.4.11，2026-11-21）：
#   tools/list 只暴露 4 个入口工具（get_instance_info/health_check/
#   search_tools/call_tool）；63 个业务工具经 search_tools 检索，
#   经 call_tool(name, arguments={request: {...}}) 调用。
#
# 行为：
#   1) initialize 握手 → notifications/initialized（stateless 传输，无 session）
#   2) tools/list：入口工具非空
#   3) tools/call get_instance_info：正向调用
#   4) 认证负向：无凭据裸调——dev 模式（MCP_DEV_MODE）预期以 dev 用户
#      成功；生产 JWT 模式预期 Authentication required
#   5) 数据层只读负向：经 execute_sql 对 odps 连接发 INSERT，预期被拒
#      （实测原生拦截：DML queries are not allowed on this database；
#       连接 allow_dml 不开即只读）
#   6) 数据层正向冒烟：execute_sql select 1——失败多为 AK 状态问题
#      （WARN 级，MaxCompute 连通性以 verify-maxcompute.py 为准）
# ============================================================
import json
import os
import sys
import urllib.error
import urllib.request

CANDIDATE_URLS = [
    os.environ.get("MCP_URL"),
    "http://step-superset-mcp:5008",  # compose 内部：容器名可解析
    "http://127.0.0.1:5008",          # 宿主机直跑
]


def _load_env_file():
    """读 .env（环境变量优先于文件）"""
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
DEV_MODE = (_ENV.get("MCP_DEV_MODE", "false").lower() == "true" and bool(_ENV.get("MCP_DEV_USERNAME")))
JWT_MODE = _ENV.get("MCP_AUTH_ENABLED", "false").lower() == "true"


def parse_body(body, content_type=""):
    """FastMCP 响应可能是纯 JSON，也可能是 SSE（event: message / data: {...}）"""
    body = body.strip()
    if not body:
        return None
    if "text/event-stream" in content_type or body.startswith("event:") or "\ndata:" in body:
        data_lines = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
        return json.loads(data_lines[-1]) if data_lines else None
    try:
        return json.loads(body)
    except Exception:
        return None


def raw_post(url, payload, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()
    except Exception as e:
        return None, {}, str(e)


class McpClient:
    def __init__(self, base):
        self.base = base
        self.next_id = 1

    def post(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self.next_id
            self.next_id += 1
        status, headers, body = raw_post(f"{self.base}/mcp", payload)
        return status, parse_body(body, headers.get("Content-Type", "")), body

    def initialize(self):
        status, parsed, body = self.post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "verify-mcp", "version": "1.0"},
        })
        if not parsed or "result" not in parsed:
            return False, f"http {status} / {body[:200]}"
        self.post("notifications/initialized", notify=True)
        return True, parsed["result"].get("serverInfo", {})

    def list_tools(self):
        _, parsed, _ = self.post("tools/list", {})
        return (parsed or {}).get("result", {}).get("tools", [])

    def call_tool(self, name, request_args):
        """经 call_tool 元工具调用业务工具（业务工具参数走 request 包装）"""
        _, parsed, _ = self.post("tools/call", {
            "name": "call_tool",
            "arguments": {"name": name, "arguments": {"request": request_args}},
        })
        return parsed

    def call_meta(self, name, arguments):
        _, parsed, _ = self.post("tools/call", {"name": name, "arguments": arguments})
        return parsed


def text_of(parsed):
    """工具执行结果的 text 内容；失败时返回 None 并填 err"""
    try:
        return parsed["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def tool_error(parsed):
    """工具执行失败（isError=true 或 JSON-RPC error）时的可读消息"""
    if parsed is None:
        return "no parsable response"
    if "error" in parsed:
        return str(parsed["error"])[:200]
    result = parsed.get("result", {})
    if result.get("isError"):
        for c in result.get("content", []):
            if c.get("type") == "text":
                return c["text"][:200]
        return "isError=true (no text)"
    return None


def main():
    global BASE_URL
    BASE_URL = next((u for u in CANDIDATE_URLS if u), None)
    if not BASE_URL:
        print("[FAIL] MCP_URL 未设置且无候选地址")
        sys.exit(1)

    # 0) 握手
    client = None
    for url in CANDIDATE_URLS:
        if not url:
            continue
        c = McpClient(url)
        ok, info = c.initialize()
        if ok:
            client, BASE_URL = c, url
            print(f"[PASS] initialize 握手 OK  server={info.get('name', '?')} "
                  f"version={info.get('version', '?')}  ({url})")
            break
    if client is None:
        print("[FAIL] 无法与 MCP 服务完成 initialize 握手（服务未启动或地址不通）")
        sys.exit(1)

    failures = 0

    # 1) tools/list：入口工具
    meta_tools = {t["name"] for t in client.list_tools()}
    if meta_tools:
        print(f"[PASS] tools/list 入口工具 {len(meta_tools)} 个：{', '.join(sorted(meta_tools))}")
    else:
        print("[FAIL] tools/list 为空或不可解析")
        failures += 1

    # 2) 正向调用（入口工具直调）
    if "get_instance_info" in meta_tools:
        parsed = client.call_meta("get_instance_info", {})
        if tool_error(parsed):
            print(f"[FAIL] get_instance_info -> {tool_error(parsed)}")
            failures += 1
        else:
            print(f"[PASS] tools/call get_instance_info -> {text_of(parsed)[:160]}")
    else:
        print("[WARN] get_instance_info 不在入口工具中，跳过")

    # 3) 认证负向：无凭据裸调（模拟全新未认证请求）
    anon = McpClient(BASE_URL)
    status, parsed, _ = anon.post("tools/call",
                                  {"name": "get_instance_info", "arguments": {}})
    rejected = parsed is None or "error" in parsed or tool_error(parsed) or (status and status >= 400)
    if JWT_MODE or not DEV_MODE:
        if rejected:
            print(f"[PASS] 未认证 tools/call 被拒 -> {str(tool_error(parsed) or status)[:160]}")
        else:
            print("[FAIL] 未认证 tools/call 竟然成功——JWT 模式下这是红线事故")
            failures += 1
    else:
        if rejected:
            print(f"[FAIL] dev 模式下未认证 tools/call 被拒（MCP_DEV_MODE/USERNAME 未生效）"
                  f" -> {str(tool_error(parsed) or status)[:160]}")
            failures += 1
        else:
            print("[PASS] dev 模式未认证 tools/call 以 MCP_DEV_USERNAME 身份执行（预期行为）")

    # 4) 业务工具经 search_tools（省略 query = 全量列表）
    real_names = []
    if "search_tools" in meta_tools:
        parsed = client.call_meta("search_tools", {})
        err = tool_error(parsed)
        if err:
            print(f"[WARN] search_tools 失败 -> {err[:160]}")
        else:
            try:
                real_names = [t["name"] for t in json.loads(text_of(parsed))]
                print(f"[PASS] search_tools 检索到 {len(real_names)} 个业务工具"
                      f"（含 execute_sql={'execute_sql' in real_names}）")
            except Exception as e:
                print(f"[WARN] search_tools 结果不可解析 -> {e}")

    # 5) 数据层只读负向 + 正向冒烟：odps 连接上 execute_sql
    if "execute_sql" in real_names and "list_databases" in real_names:
        parsed = client.call_tool("list_databases", {})
        err = tool_error(parsed)
        odps_id = None
        if err:
            print(f"[WARN] list_databases 失败 -> {err[:160]}")
        else:
            try:
                rows = json.loads(text_of(parsed)).get("databases", [])
            except Exception:
                rows = []
            for d in rows:
                if d.get("backend") == "odps" or "ODPS" in str(d.get("database_name", "")).upper():
                    odps_id = d.get("id")
                    print(f"[PASS] 定位 odps 连接: id={odps_id} name={d.get('database_name')}")
                    break
            if odps_id is None:
                print("[WARN] 未找到 odps 连接，跳过数据层用例")

        if odps_id is not None:
            neg_sql = "INSERT INTO __mcp_verify_negative_table__ VALUES (1)"
            parsed = client.call_tool("execute_sql", {"database_id": odps_id, "sql": neg_sql})
            err = tool_error(parsed)
            err_text = err or text_of(parsed) or ""
            if err or '"success":false' in err_text:
                print(f"[PASS] execute_sql INSERT 被拒（数据层只读兜底生效）"
                      f" -> {err_text[:160]}")
            else:
                print("[FAIL] execute_sql INSERT 竟然成功——请立即检查连接 allow_dml 与 AK 权限！")
                failures += 1

            parsed = client.call_tool("execute_sql", {"database_id": odps_id, "sql": "select 1"})
            err = tool_error(parsed)
            body_text = err or text_of(parsed) or ""
            if err:
                print(f"[WARN] execute_sql 'select 1' 被拒 -> {body_text[:160]}")
            elif '"success":true' in body_text:
                print("[PASS] execute_sql 'select 1' 正向冒烟 OK")
            else:
                # 常见：AccessKeyStatusError（AK 禁用/失效）——环境问题，非 MCP 链路问题
                print(f"[WARN] execute_sql 'select 1' 未成功（多为 MaxCompute 凭据状态，"
                      f"以 verify-maxcompute.py 为准）-> {body_text[:160]}")
    else:
        print("[WARN] 无 execute_sql/list_databases 业务工具，跳过数据层用例")

    print("done:", "全部通过" if failures == 0 else f"{failures} 项失败")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
