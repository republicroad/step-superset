# 本地测试 / 内网环境专用：把 duckdb 从 Superset 方言黑名单摘除
#
# 背景：superset/security/analytics_db_safety.py 默认拦截 duckdb、sqlite、
# shillelagh 三类方言，理由是 duckdb 的文件读取函数（read_parquet 等）
# 等价于放开本地文件系统访问。
#
# 解除前提（缺一不可）：
#   1. 明确信任所有能访问 Superset 的用户
#   2. 配合 docs/duckdb.md 的三层闸门配置收窄实际可读范围
#   3. 生产/多租户环境不要解除，改走专用服务型数据库
import pathlib

p = pathlib.Path("/app/superset/security/analytics_db_safety.py")
t = p.read_text()
needle = r're.compile(r"duckdb(?:\+[^\s]*)?$"),'
assert needle in t, "duckdb blocklist pattern not found"
p.write_text(t.replace(needle, "# " + needle + "  # enabled for trusted test env"))
print("patched:", p)
