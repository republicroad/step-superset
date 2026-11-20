# Quack 远程协议：DuckDB 客户端-服务端访问

> 覆盖：协议要点、三种客户端用法、实测注意事项、与 Superset 的结合方式。
> 结论：**quack 是 DuckDB 2.0 官方的远程访问协议**（HTTP 传输、默认端口 9494、
> `quack:` URI、token 认证），能把 DuckDB 实例变成多客户端可访问的服务端；
> `quack_query` 表函数让 Superset 虚拟数据集可以直接查远程 DuckDB/DuckLake，零初始化钩子依赖。
> 版本锚定：duckdb 2.0.0（协议打磨版），行为实测于 2026-11-19。

## 1. 协议要点（官方文档核实 2026-11）

| 项 | 值 |
|---|---|
| 传输 | HTTP / HTTPS（可用反代做 TLS 终结） |
| 默认端口 | 9494 |
| URI 格式 | `quack:<host>[:port]`，本机地址自动用明文 HTTP，非本机默认 HTTPS |
| 序列化 | DuckDB 内部序列化格式，复杂类型（嵌套/decimal/interval）无损 |
| 认证 | token（`quack_serve` 生成，或 `token = '...'` 自指定）；可配 secret 作用域 |
| 会话能力 | DDL / INSERT / 事务都转发到服务端；查询单次往返，大结果分块流式返回 |

## 2. 服务端

```sql
CALL quack_serve('quack:localhost', token = '...');   -- 默认只监听本机名
CALL quack_serve('quack:0.0.0.0:9494', token = '...', allow_other_hostname = true);  -- 对外
CALL quack_stop('quack:localhost');
```

要点（实测）：

1. **quack 不会自动安装**——文档称首次使用自动安装加载，实测必须显式
   `INSTALL quack; LOAD quack;`
2. **不要消费 `quack_serve` 的返回行**——实测 `fetchone()` 会阻塞；token 用命名参数指定
3. 服务生命周期绑定启动它的连接/进程，进程退出服务即停

CLI 捷径：`duckdb -serve` 直接起服务、`duckdb -connect 'quack:...'` 直连。

## 3. 客户端三种用法（`scripts/quack_client.py`）

```sql
-- ① 无状态：单条 SQL，不建会话
FROM quack_query('quack:host', 'SELECT 42', token = '...');

-- ② ATTACH：远端整个目录挂载，DDL/INSERT/事务全部转发
ATTACH 'quack:host' AS remote (TOKEN '...');
SELECT count(*) FROM remote.shared.t;      -- 非 main schema 要写全限定名

-- ③ CONNECT：整个会话路由到远端执行（官方推荐），DISCONNECT 回本地
CONNECT remote;
SELECT count(*) FROM t;
DISCONNECT;
```

实测四项全部通过：无状态查询返回 42；ATTACH 后远端表可查（10 行）；
过滤下推在远端计算；`FROM remote.query('FROM whoami()')` 可读服务端身份
（版本、平台、uptime）。

## 4. 与 Superset 结合

`quack_query` 是**表函数**，可以直接写进虚拟数据集 SQL：

```sql
SELECT * FROM quack_query('quack:<host>',
                          'SELECT ... 远端 SQL ...',
                          token = '...',
                          disable_ssl = true);   -- 非本机明文 HTTP 时
```

- 不依赖任何连接初始化钩子（对比 DuckLake 的 ATTACH 困境，见 docs/ducklake.md）
- Superset 进程不需要对远端数据的任何文件/catalog 访问权限
- token 会出现在 SQL 文本里，注意数据集的可见范围；更稳的做法是用 quack secret
  （客户端 `CREATE SECRET (TYPE quack, TOKEN ..., SCOPE 'quack:host')`），
  但 secret 同样受「每个连接独立实例」影响，按连接创建路径评估

前提：Superset 所在镜像已解除 duckdb 方言黑名单（见 docs/duckdb.md 第 2 节），
且远端服务器可达（容器网络下注意 host 名解析）。

## 5. 已知边界（实测）

- quack 客户端同样需要显式 `INSTALL quack; LOAD quack;`（autoload 实测未生效）
- `ATTACH` 绑定远端表定义时，单表绑定失败会导致整个 ATTACH 失败
  （远端有懒解析表如 Iceberg catalog 时可能触发，官方 troubleshooting 章节有专述）
- 非本机地址默认 HTTPS，需要自签证书或反代；测试环境用 `disable_ssl = true`
