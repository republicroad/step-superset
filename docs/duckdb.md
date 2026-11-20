# Superset 连接 DuckDB

> 覆盖：镜像集成、方言黑名单补丁、连接形态、三层闸门安全配置、S3/OSS 进阶。
> 结论：官方镜像不带 duckdb 驱动，需派生镜像；7.0 默认拦截 duckdb 方言（本仓库镜像已打补丁解除，
> 生产环境不要解除）；本地目录安全用 `allowed_directories` + 总闸 + 上锁三层闸门收敛。
> 版本锚定：Superset 7.0.0rc2 + duckdb 2.0.0 + duckdb-engine 0.17.0，行为实测于 2026-11-19/20。

## 1. 镜像集成三个坑

官方镜像不带 pyodps / duckdb-engine，Dockerfile 已处理，坑记录如下（全部实测）：

| 坑 | 现象 | 解法 |
|---|---|---|
| 装错 Python | `pip install` 成功但运行时 `import odps` / `import duckdb` 失败 | 运行时解释器是 `/app/.venv/bin/python`，PATH 上的 pip 指向系统 Python；必须 `pip --python /app/.venv/bin/python install` |
| venv 没有 pip | `ensurepip` 引导也失败 | 跳过 ensurepip，直接用系统 pip 的 `--python` 参数 |
| 无写权限 | 构建段写 site-packages 权限拒绝 | 镜像默认 `USER superset`，构建段切 `USER root`，装完切回 |

另两个细节：podman 构建产物完整名带 `localhost/` 前缀，compose 引用要写全；
`pip install pyodps` 的导入名是 `odps`（验证别写 `import pyodps`）。

## 2. 方言黑名单与补丁边界

Superset 的 `analytics_db_safety` 模块默认拦截 duckdb / sqlite / shillelagh 三类方言，
理由：duckdb 的文件读取函数（`read_parquet` 等）等价于放开本地文件系统访问。
本仓库镜像已打补丁（`patch/enable_duckdb.py` 注释掉黑名单条目）——**仅限测试/内网环境**；
生产/多租户不要解除，主 BI 数据源改用服务型数据库，或走专用隔离架构。

## 3. 连接形态

| 形态 | URI | 适用 |
|---|---|---|
| 持久数据库 | `duckdb:////data/duckdb/main.duckdb` | 持久视图/表（本仓库默认，数据来自 `scripts/prepare-ducklake-demo.py`） |
| 内存库 | `duckdb:///:memory:` | 每个连接独立内存实例，只适合无状态演示 |
| 直读文件 | 虚拟数据集 SQL `SELECT * FROM '/data/duckdb/data/*.parquet'` | 本地文件；远程对象存储需先加载 httpfs |

注意：duckdb 引擎下每个 SQLAlchemy 连接是**独立的 duckdb 实例**（实例级设置重连即重置），
内存库形态连表都共享不了。

## 4. 三层闸门安全配置（核心）

| 设置 | 作用 | 默认 |
|---|---|---|
| `enable_external_access` | 总闸：`COPY` / `ATTACH` 文件库 / `read_csv` / `read_parquet` / 加载扩展全部禁止 | `true` |
| `allowed_directories` | 目录与 URL 前缀白名单，总闸关闭后仍放行 | `[]` |
| `allowed_paths` | 单文件白名单，总闸关闭后仍放行 | `[]` |
| `disabled_filesystems` | 按文件系统名禁用，如 `LocalFileSystem` | `''` |
| `lock_configuration` | 上锁后一切 `SET` 被拒（`allowed_configs` 名单内除外） | `false` |

四条铁律（实测，duckdb 2.0.0 验证）：

1. **顺序不可反**：先写白名单、再关总闸；关闸后名单不能再改
2. **总闸单向**：关上打不开（`Cannot enable external access while database is running`），要留调整余地只能在连接建立后通过钩子配置
3. **`lock_configuration = true` 放最后**：不上锁时其余限制可被 SQL 改写，锁了才是硬约束
4. **`allowed_directories` / `allowed_paths` 不能在连接创建时设置**：实测 connect config
   方式被拒（`Cannot change/set allowed_directories before the database is started`）——
   白名单只能在实例启动后逐条 `SET`（铁律 1 的"顺序"由此而来）

机制为实例级：每个连接一个实例，`SET` 只影响当前实例，重连即重置——
因此闸门必须每次连接重建。

### 4.1 最终生效的完整配置序列（本仓库实际执行，学习白名单设置看这里）

每个连接建立后，安全钩子（`docker/duckdb_engine_security.py`）按以下顺序逐条执行
（凭据来自挂载的 `/app/.env`，AK/Secret 以占位符表示）：

```sql
SET extension_directories = ['/opt/duckdb-extensions'];
LOAD httpfs;                                              -- 从预烘焙目录加载扩展
SET allow_persistent_secrets = false;                     -- 禁止 secret 落盘
CREATE SECRET s3_security (TYPE s3,
    KEY_ID '<AK>', SECRET '<SK>',
    ENDPOINT 'oss-cn-shenzhen.aliyuncs.com',
    REGION 'cn-shenzhen');                                -- 会话内 secret，仅存内存
SET ca_cert_file = '/etc/ssl/certs/ca-certificates.crt';
SET allowed_paths = ['/etc/ssl/certs/ca-certificates.crt'];
SET allowed_directories = ['s3://trackdata', '/data/duckdb'];
SET enable_external_access = false;                       -- 关总闸
SET autoload_known_extensions = false;
SET autoinstall_known_extensions = false;
SET allow_community_extensions = false;
SET lock_configuration = true;                            -- 上锁：之后任何 SET 被拒
```

逐行学习要点：

| 语句 | 学到什么 |
|---|---|
| `SET extension_directories` | 先指向扩展目录，`LOAD` 才能离线找到 httpfs（不设则找 `~/.duckdb/extensions`） |
| `LOAD httpfs` 在最前 | 关总闸后 `LOAD` 被拦（`Loading external extensions is disabled`）——扩展加载永远在闸门之前 |
| `allow_persistent_secrets = false` | 持久 secret 明文落盘，敏感环境必关；secret 只存会话内存 |
| `CREATE SECRET` 在关闸前 | secret 创建走"代码通道"，与数据白名单是独立通道，但同样会被总闸拦 |
| `ca_cert_file` + `allowed_paths` | HTTPS 握手要读本地 CA 文件，总闸关闭后 CA 文件自身也要进单文件白名单 |
| `allowed_directories` | **目录级**白名单（目录与 URL 前缀），单文件用 `allowed_paths` |
| `enable_external_access = false` | 总闸：名单外的一切文件/ATTACH/COPY 全拦 |
| 扩展三连关 + 上锁 | 扩展通道纵深防御；不上锁时上述限制可被 SQL 改写，锁了才是硬约束 |

### 4.2 四条铁律（实测，duckdb 2.0.0 验证）

1. **顺序不可反**：白名单先于总闸；扩展加载先于一切
2. **总闸单向**：关上打不开（`Cannot enable external access while database is running`）
3. **`lock_configuration = true` 放最后**：不上锁时其余限制可被 SQL 改写，锁了才是硬约束
4. **`allowed_directories` / `allowed_paths` 不能在连接创建时设置**：connect config 方式实测被拒
   （`Cannot change/set allowed_directories before the database is started`）——
   只能在实例启动后逐条 `SET`。这直接否决了「connect_args 预置闸门」方案，
   duckdb-engine 又没有 post-connect 初始化钩子，所以本仓库用
   `docker/duckdb_engine_security.py` monkeypatch 方言的 `connect()` 补执行时机

### 4.3 机制与实现

设置全部为**实例级**：`SET` 只影响当前实例，重连即重置，闸门必须每次连接重建。
执行者是 `docker/duckdb_engine_security.py`（monkeypatch `Dialect.connect`，每连接
自动重放 4.1 序列），由 `.env` 的 `DUCKDB_SECURITY_HOOK=true`（默认）或
`DUCKDB_S3_ENABLED=true` 在 `docker/superset_config.py` 中激活；profile 由 `.env`
内容自动决定：配置了 `DUCKDB_S3_*` 走 4.1 完整序列，仅 `DUCKDB_ALLOWED_DIRECTORIES`
则跳过 httpfs/secret/CA 相关语句（本地白名单 profile）。

## 5. 负向验收（安全配置的验收标准）

配好后必须跑两个负向测试，全部被拦才算配对（`verify-duckdb.py` 已内置）：

```sql
SELECT count(*) FROM read_csv('/etc/passwd');   -- 预期：file system operations are disabled
ATTACH '/etc/hostname' AS smuggled;             -- 预期：同上
```

## 6. 对象存储（S3 / OSS 兼容端点）进阶

总闸关闭后，S3 读取被连带禁止，放行需要成套配置，顺序敏感：

```sql
INSTALL httpfs;                                -- ① 必须在关闸前：关闸后 LOAD 被拦
LOAD httpfs;
SET allow_persistent_secrets = false;          -- ② 凭证只留内存（持久 secret 目录会被闸拦，且明文落盘本就不该开）
CREATE SECRET aws (TYPE s3, KEY_ID '...', SECRET '...');
SET ca_cert_file = '/etc/ssl/certs/ca-certificates.crt';  -- ③ HTTPS 握手要读 CA 文件
SET allowed_paths = ['/etc/ssl/certs/ca-certificates.crt'];
SET allowed_directories = ['s3://<bucket>/<prefix>/'];    -- ④ 前缀匹配，可限定到目录级
SET enable_external_access = false;
SET autoload_known_extensions = false;         -- ⑤ 扩展通道纵深防御（与数据白名单是独立通道）
SET autoinstall_known_extensions = false;
SET allow_community_extensions = false;
SET lock_configuration = true;
```

注意：以上是**通用配方**（CLI / 独立会话场景，手动逐条执行）。本仓库 Superset
场景的最终生效序列见 4.1 节——差异点：扩展从镜像预烘焙目录加载（`SET
extension_directories` 先行，无需 INSTALL 联网下载），secret 名为 `s3_security`
且参数由 `.env` 注入，白名单同时含 S3 前缀与本地目录。

实测闭环口径：配好后对允许前缀的 `read_parquet('s3://...')` 用假凭证应收到存储端返回的
403 类错误（请求真实发出，说明闸门、TLS、凭证链路全通）；前缀外对象被
`file system operations are disabled` 拦截。

**本仓库的实现**：上述序列包含 `LOAD httpfs` 这类 post-connect 语句，而
duckdb-engine 没有 post-connect 初始化钩子，直接塞 connect_args 会因「关闸后 LOAD 被拦」失败。
仓库内已内置 S3 配方的完整实现，三件套：

1. `docker/duckdb_engine_security.py`——monkeypatch duckdb_engine 的 `Dialect.connect`，
   每个连接建立后按上述顺序逐条执行（失败即抛出，宁可连接失败也不静默放开）
2. `docker/superset_config.py`——读 `/app/.env`，仅当 `DUCKDB_S3_ENABLED=true` 时
   激活钩子（不启用时行为与官方镜像完全一致）
3. Dockerfile——预烘焙 httpfs 扩展到 `/opt/duckdb-extensions`（关总闸后无法联网安装，
   LOAD 依赖扩展已在本地）

启用方式：`.env` 设 `DUCKDB_S3_ENABLED=true` 并填入
`DUCKDB_S3_ENDPOINT / REGION / KEY_ID / SECRET / BUCKET_PREFIX`，重建镜像并重启。

验收口径（S3 profile 专属，**本仓库已实测**，2026-11-20）：对白名单前缀用假凭证发起
`read_parquet('s3://...')`，实测收到真实存储端返回的
`HTTP 403 Forbidden / InvalidAccessKeyId`（请求穿过闸门、TLS、凭证链路打到 AWS，
链路贯通的证明）；白名单外对象实测被
`file system operations are disabled` 拦截。本地基线（第 4 节）同步全量验证：
本地白名单内查询成功、`/etc/passwd` 与名单外 ATTACH 被拦。
四用例验收脚本：`scripts/verify-duckdb-chain.py`。
真实凭据读取也已实测：`read_json('s3://trackdata/track/.../*.log.gz')` 经 Superset
查询管道返回真实事件数据（10 行 × 32 列，含嵌套 STRUCT）。

### OSS 区域与 DNS 两个坑（实测）

1. **ENDPOINT 区域必须与桶所在区域一致**：深圳桶配杭州端点时，duckdb 会跟随 OSS 的
   301 PermanentRedirect 重定向（CLI 下表现为"能用"），但经 Superset 查询管道时
   重定向不被跟随，直接报 `PermanentRedirect`；深圳域名首次解析可能瞬时失败
   （CNAME 链 `*.oss-adns.aliyuncs.com`），重试即可
2. 走 httpfs 的读路径在挂载文件更新后需**重启容器**才生效（superset_config
   的钩子在启动时接线，Python 字节码缓存可能滞后）

## 7. 白名单不是只读

名单内路径可读**也可写**（实测 `COPY (SELECT ...) TO '<白名单内>/x.csv'` 直接成功）。
只读保护交给 OS 层：数据目录只读挂载 + 容器以非 root 用户运行。

## 8. 已知怪癖

- 设置白名单后，`temp_directory` 会被自动追加进 `allowed_directories`（默认 `.tmp`
  相对当前工作目录解析）；不信任工作目录时显式 `SET temp_directory` 指到受控路径
- `disabled_filesystems` 写入有效但 `current_setting()` 读回空字符串，验证要靠功能行为
- v1.5.x 在未加载 httpfs 时写 `s3://` 白名单条目会被静默写成相对路径（v2.0.0 已修复）；
  名单含远程前缀时，`LOAD httpfs` 永远写在白名单设置之前
