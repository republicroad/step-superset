# ============================================================
# Quack 服务端演示：把 DuckDB 实例变成 quack 远程协议服务端
#
# 用法（容器或任意装有 duckdb 的环境）：
#   python quack_server.py            # 监听 quack:localhost:9494
# 配合 quack_client.py 使用；服务生命周期绑定本进程/本连接
# ============================================================
import os
import time

import duckdb

# token 由环境变量给定（演示值），客户端必须携带同一 token
TOKEN = os.environ.get("QUACK_TOKEN", "LOCALTESTTOKEN01234567890ABCDEF")

con = duckdb.connect()
con.execute("INSTALL quack")   # 实测：quack 不会自动安装，必须显式 INSTALL
con.execute("LOAD quack")
con.execute("CREATE SCHEMA shared")
con.execute("CREATE TABLE shared.t AS SELECT * FROM range(10) r(i)")

# 注意：不要消费 quack_serve 的返回行（实测 fetchone 会阻塞），
# token 用命名参数直接指定
con.execute(f"CALL quack_serve('quack:localhost', token = '{TOKEN}')")
print("quack server started on quack:localhost:9494, keeping process alive", flush=True)
while True:
    time.sleep(1)
