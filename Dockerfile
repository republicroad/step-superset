# ============================================================
# Superset 派生镜像：官方镜像 + pyodps（MaxCompute）+ duckdb-engine（DuckDB）
#
# 版本切换：compose 传参或在 .env.local 设 SUPERSET_TAG
#   --build-arg SUPERSET_TAG=7.0.0rc2     当前实测基线
#   --build-arg SUPERSET_TAG=7.0.0        7.0 正式版发布后
#
# 构建：podman build --http-proxy=false -t localhost/superset-mc:7.0.0rc2 .
# ============================================================

ARG SUPERSET_TAG=7.0.0rc2
FROM apache/superset:${SUPERSET_TAG}

# 构建段切 root：镜像默认 USER superset，无权写 /app/.venv 的 site-packages
USER root

# 关键：Superset 镜像运行时用 /app/.venv（无 pip，ensurepip 引导失败），
# 必须用系统 pip 的 --python 参数向 venv 安装；
# 直接 `pip install` 会装进系统 site-packages，运行时 import 不到
RUN /usr/local/bin/pip --python /app/.venv/bin/python install \
    --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    pyodps duckdb-engine

# 预烘焙 httpfs 扩展（S3/OSS 安全配方的 LOAD 依赖；关总闸后无法再联网安装）
# 安装到共享目录 /opt/duckdb-extensions，运行时通过 extension_directories 设置寻址
RUN python -c "import duckdb, os; os.makedirs('/opt/duckdb-extensions', exist_ok=True); con = duckdb.connect(':memory:', config={'extension_directory': '/opt/duckdb-extensions'}); con.execute('INSTALL httpfs')"

# 可选：其他数据源驱动（Doris / StarRocks / Trino），按需取消注释并重建
# RUN /usr/local/bin/pip --python /app/.venv/bin/python install \
#     --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple pydoris starrocks trino

# 本地测试/内网环境：解除 duckdb 方言黑名单
# （Superset 安全模块 analytics_db_safety 默认拦截 duckdb 方言，
#  理由是 duckdb 的文件读取函数等价于本地文件系统访问）
COPY patch/enable_duckdb.py /tmp/enable_duckdb.py
RUN python /tmp/enable_duckdb.py

# 恢复镜像默认运行用户
USER superset
