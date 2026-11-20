# ============================================================
# 重建 DuckDB 演示数据（容器内运行，写 /data/duckdb）
#
# 用法（在仓库根目录，Git Bash / 任何 shell）：
#   podman exec -i step-superset-app python - < scripts/demo/prepare-ducklake-demo.py
#
# 产出（全部可由本脚本重建，git 不追踪二进制）：
#   /data/duckdb/main.duckdb        主库：ops.demo 视图（指向 parquet 文件）
#   /data/duckdb/data/part*.parquet 两个演示分区文件
#   /data/duckdb/catalog.db         DuckLake catalog（CLI 侧 ATTACH 演示用）
# ============================================================
import os

import duckdb

BASE = os.environ.get("DEMO_DIR", "/data/duckdb")
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

con = duckdb.connect(os.path.join(BASE, "main.duckdb"))

# 1) 两个 parquet 分区文件（模拟湖内数据文件）
con.execute(
    "COPY (SELECT * FROM (VALUES (1,42,'a'),(2,43,'b'),(3,44,'c')) t(id,x,tag)) "
    f"TO '{DATA}/part1.parquet' (FORMAT PARQUET)"
)
con.execute(
    "COPY (SELECT * FROM (VALUES (4,45,'d'),(5,46,'e')) t(id,x,tag)) "
    f"TO '{DATA}/part2.parquet' (FORMAT PARQUET)"
)

# 2) 持久化视图：指向 parquet 通配路径，查询时现读文件
#    这是 Superset 查湖内文件最稳的形态（视图定义持久，无 DuckLake 也可用）
con.execute("CREATE SCHEMA IF NOT EXISTS ops")
con.execute(f"CREATE OR REPLACE VIEW ops.demo AS SELECT * FROM '{DATA}/*.parquet'")

# 3) DuckLake catalog 演示（ ATTACH 不跨会话持久，供 CLI 侧体验流程用 ）
con.execute(f"ATTACH 'ducklake:sqlite:{BASE}/catalog.db' AS lake (DATA_PATH '{DATA}')")
con.execute("CREATE SCHEMA IF NOT EXISTS lake.ops")
con.execute("CREATE OR REPLACE TABLE lake.ops.demo AS SELECT * FROM ops.demo")
print("ducklake catalog written: catalog.db (lake.ops.demo)")

rows = con.execute("SELECT * FROM ops.demo ORDER BY id").fetchall()
print("ops.demo rows:", rows)
con.close()

# 4) 重开验证视图持久性
con = duckdb.connect(os.path.join(BASE, "main.duckdb"))
print("reopen ops.demo count:", con.execute("SELECT count(*) FROM ops.demo").fetchall())
con.close()
print("done. files:", sorted(os.listdir(BASE)))
