# ============================================================
# Quack 客户端演示：三种远程访问用法（quack:localhost:9494）
#
# 前置：quack_server.py 已在某进程/容器运行，QUACK_TOKEN 一致
# 用法：python quack_client.py
# ============================================================
import os

import duckdb

TOKEN = os.environ.get("QUACK_TOKEN", "LOCALTESTTOKEN01234567890ABCDEF")

con = duckdb.connect()
con.execute("INSTALL quack")   # 实测：必须显式 INSTALL（文档所称 autoload 实测未生效）
con.execute("LOAD quack")

# 用法 1：无状态查询（单条 SQL，不建会话）
rows = con.execute(
    f"FROM quack_query('quack:localhost', 'SELECT 42 AS remote_answer', token='{TOKEN}')"
).fetchall()
print("1) quack_query:", rows)

# 用法 2：ATTACH —— 远端整个目录挂载（DDL/INSERT/事务都能转发）
# 注意：非 main schema 的表要写全限定名 remote.shared.t
con.execute(f"ATTACH 'quack:localhost' AS remote (TOKEN '{TOKEN}')")
print("2) ATTACH 后查远端表:",
      con.execute("SELECT count(*) FROM remote.shared.t").fetchall())
print("3) 过滤下推:",
      con.execute("SELECT sum(i) AS total FROM remote.shared.t WHERE i > 5").fetchall())

# 用法 3：CONNECT —— 整个会话路由到远端（文档推荐用法，实测见 docs/quack.md）
print("4) whoami:", con.execute("FROM remote.query('FROM whoami()')").fetchall()[:1])

con.execute("DETACH remote")
print("DETACH OK")
