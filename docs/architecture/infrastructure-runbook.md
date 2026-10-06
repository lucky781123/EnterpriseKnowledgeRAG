# 异步入库、在线兜底与备份恢复操作

## 配置边界

- 本地 factory 只支持 development；`config/worker-identities.toml` 是每阶段重读的
  演练身份目录，不是 OIDC。生产必须注入正式身份与 membership resolver。
- 默认旧同步 `POST /api/v1/documents` 保留。新增
  `POST /api/v1/ingestion/tasks` 返回 202；GET `/{task_id}` 查 PG 状态；
  POST `/{task_id}/replay` 只重放死信，owner/tenant 隔离。
- 新入口单文件上限 100 MiB，限流每租户 burst 10、恢复速率 2/s。
  不对旧同步 API 的无上限承诺作变更。
- `RAG_INFRA_ENABLED`、`RAG_CELERY_ENABLED` 默认 false。
  模型/profile、协调 Redis、存储配置需要 API/Worker/dispatcher 一致。
- worker stage JSON 消息仅有 `event_id`。Celery backend 不是任务事实源，
  task 状态/重试/死信/checkpoint/fence/outbox 全部以 PostgreSQL 为准。

## DeepSeek 密钥

真实 API key 填在项目 `.local/secrets/deepseek-api-key.txt`，文件权限 0600；
不要把 key 写到 TOML、命令参数、日志或 Git。`sk-xxxxxxxxxxx` 是无效占位。

`config/deepseek.toml` 已配置用户指定 endpoint 与模型：
`https://api.aiportx.com/v1`、`deepseek-v4.1-flash`。
填 key 后把 `enabled=true`，并把明确允许外发的 tenant 放入 `allowed_tenants`。
`allowed_tenants` 填服务端身份的 tenant ID，不是用户名、模型名或 API key。
`local-demo` 可能已有私人文档，不应为了连通性测试直接放行整个租户。
业务文档必须先明确外发授权，不能先发到网关再靠输出过滤补救。
空 allowlist 或无可信产品上下文只调用 LM Studio。超时/限流/不可用最多兜底一次；
认证/协议错误不静默掩盖，首个流事件后禁止透明切换，deadline 不重置。

### 公开样本联调

可以在业务 allowlist 仍为空时执行：

```bash
python -m scripts.deepseek_smoke
python -m scripts.deepseek_smoke --execute
```

默认仅展示计划；`--execute` 才读取独立密钥并调用模型。不自动读取 `.env`。
脚本仅使用源码中固定公开合成文本，测试进程内临时放行 `deepseek-public-smoke`，
不修改业务 TOML、身份目录、现有 API 或数据库，也不读取已有文档。
两次真实在线调用分别验证非流式与 SSE、结构化回答和逐字引用；
两次 HTTP 503 **故障注入**验证真实 LM Studio 单次兜底；
未允许 tenant 与无可信 tenant context 验证不触发在线 Provider。
本地模型地址仅允许 loopback，当前无需本地模型密钥；启用本地认证后需另行装配。

只归档状态、耗时和调用次数到 `.local/deepseek-smoke/result.json`，不保存密钥、
正文或 reasoning。在线失败后即使本地兜底成功，在线检查仍为失败。
2026-10-06 六项均通过，非流式约3.10秒、SSE约3.47秒；
证据 `artifacts/experiments/2026-10-06_deepseek-public-smoke/verification.json`。
这是 Provider 联调，不是完整异步入库/检索/在线生成/ACL 产品验收。
业务租户在正式授权并配置前仍走本地模型。

完整公开 RAG 演练另用 `python -m scripts.infrastructure_smoke --online-public`。
必须显式设置 `INFRA_SMOKE_ISOLATED=true`、loopback `infra_test*` PostgreSQL、
独立 Redis/Weaviate/对象目录、匿名模式关闭、真实本地三模型和基础设施开关；
不从 `.env` 加载配置，不使用业务服务。在线演练拒绝已有文档的数据库，
Celery 使用 `scripts.infrastructure_smoke_worker:app`，其身份 resolver 仅授权
`deepseek-public-smoke/public-smoke-user`，Worker 自身也检查隔离数据库。
2026-10-06 真实异步202/六阶段发布/对象hash/pointer/混合检索/Jina/DeepSeek
闭环通过，answered/引用passed、在线1次/兜底0次；跨tenant404与撤权后404/
在线次数不变均通过。补齐Worker隔离守卫后复跑通过。
本次对象存储为local、macOS池为solo，不代表托管MinIO或Linuxprefork验收；
临时容器已关闭，业务服务和allowlist不变。

## 启动与迁移

### 本机工作台验收

现有开发 `.env` 配置可通过 `bash start.sh start-async` 加载，仅进入进程环境，
不打印配置；`bash start.sh async-status` 查API/Worker/dispatcher，
`bash start.sh async-stop` 只停本项目辅助进程与API。常规start/stop主要管理API。
此入口为development专用，不代替下述生产迁移装配。

缺少双Redis配置时以`compose.local-async.yaml`新建项目独立Redis7.4，
loopback随机端口与持久卷、不占其他项目6379；已有配置必须完整，不混用。
显式迁移0005、Worker/dispatcher共享模型/profile/存储与Redis设置，
只重启登记归属的API以加载新配置。项目内可选SDK目录复用，不改Conda或安装模型。
入库专用Ollama地址未配时复用本机查询模型，不宣称资源已隔离或满足生产容量。

UI异步模式只在infra.enabled时启用：上传返回202/Task ID，查询阶段和重试状态，
只有succeeded标完成；dead_letter显示失败。用户可用API人工replay，
现阶段没有在UI新增replay按钮。旧同步API与旧Document ID查询保留。
当前地址8001/product-workbench/，无Token本机身份local-demo/local-demo-user；
在线allowlist保持空，使用LM Studio。托管MinIO尚未配置，保持local对象存储。
公开样本实际受理0.047秒、发布28.387秒、HTTP与浏览器问答均引用passed。
本次服务留运行供用户验收，不清理业务数据和Redis持久卷。

启动进程显式注入配置，不自动读取 `.env`。安装项目 extra：

```bash
python -m pip install '.[infrastructure]'
python -m scripts.migrate_async_ingestion
uvicorn scripts.local_p0_api:app --factory --host 127.0.0.1 --port 8002 --no-proxy-headers
celery -A scripts.celery_worker:app worker --pool=prefork --concurrency=1 --loglevel=WARNING
python -m scripts.ingestion_runtime
```

macOS 本地测试用 `--pool=solo`，Linux 容器用 prefork。API 启动检查显式 0005，
默认 migration CLI 仍为 0004，不自动迁移。0005 只新增表，不破坏旧表。
容器入口 `compose.infrastructure.yaml` 是独立 development 装配，要求显式
`RAG_RUNTIME_ENV_FILE`。模型使用独立 `config/container-llm.toml` 的
`host.docker.internal`，不修改本机配置。入库专用 `INGESTION_OLLAMA_BASE_URL`
也须可从容器访问。Redis 不发布端口，生产还需 TLS/ACL。

## 原文迁移

先停止旧版不参与门禁的 API/脚本，启用所有写入口的门禁，然后执行：

```bash
python -m scripts.migrate_object_storage --source .local/p0-object-storage
python -m scripts.migrate_object_storage --source .local/p0-object-storage --execute
```

仅复制原文和全部历史 manifest，不删除源，不修改 PG locator。目标同 key 同 hash
可复用，异内容拒绝。核验报告后切换 `OBJECT_STORAGE_PROVIDER=s3`，重启 API/Worker，
再核验上传、查询、ACL/撤权和 manifest。源目录保留作为回滚，不并行写两套存储。

## 四系统备份

需要已安装且版本兼容的 `pg_dump`、`pg_restore`、`redis-cli`、`redis-check-rdb`；
托管 Weaviate 必须支持已授权的原生 backup backend。当前不擅自启用托管模块。
备份目录只允许项目内，不提交备份到 Git。

```bash
python -m scripts.backup_restore backup --directory .local/backups/run-001
python -m scripts.backup_restore backup --directory .local/backups/run-001 --execute
python -m scripts.backup_restore verify --directory .local/backups/run-001
```

执行前冻结全部业务写入口，停止旧脚本、Celery Worker 和 dispatcher，确认所有在途
任务退出/租约到期。备份持 PG exclusive advisory gate；任何外部不参与门禁的写者
都必须停掉。缓存与 broker 自身可能写 TTL/ACK，不得宣称跨系统分布式事务。
PG 是队列恢复事实源；恢复后先恢复 PG outbox，再恢复 broker，允许重复投递。

清单完成标记仅在 PG custom dump、MinIO 当前对象、两实例 Redis RDB、
Weaviate 原生快照 SUCCESS 后写入。MinIO **不包含历史版本/删除标记/服务端 bucket
策略**；这些需服务商版本化备份和配置导出，未提供权限前明确待验证。
Weaviate 快照仍在服务商 backup storage，需单独验证保留期、跨实例恢复权限、
加密和异地副本；本地 manifest 不等于异地备份。备份目录应放加密卷、权限 0700。

## 分阶段隔离恢复

配置所有 `RESTORE_POSTGRES_DSN`、`RESTORE_S3_*`、`RESTORE_WEAVIATE_*`、
`RESTORE_REDIS_CACHE_URL`、`RESTORE_REDIS_COORDINATION_URL`，不能与源 identity 相同。
准备空隔离 PG、空 MinIO bucket、空 Weaviate；Redis 目标离线且两个本地数据目录为空。
目标 Weaviate 必须能访问源 backup storage，Redis RDB 必须使用兼容版本。

```bash
python -m scripts.backup_restore restore --directory .local/backups/run-001
python -m scripts.backup_restore restore --directory .local/backups/run-001 \
  --cache-data .local/restore/cache --coordination-data .local/restore/coordination --execute
# 将恢复的 dump.rdb 作为两个目标 Redis 的启动数据，先以 appendonly no 启动加载 RDB。
# 确认加载和数据后再 CONFIG SET appendonly yes，等待 AOF rewrite 完成并持久化启动配置。
# 协调实例必须 noeviction。直接 appendonly yes 启动可能优先读取空 AOF 而忽略 RDB。
python -m scripts.backup_restore restore-verify --directory .local/backups/run-001 --execute
```

顺序 PG→MinIO→Weaviate→离线 Redis。任何失败都不开放业务。
恢复 CLI 不使用 `--clean`、不删除已有表/对象、不自动启动服务。
Redis 数据目录不得残留旧 AOF；先加载 RDB，再开启并完成 AOF 重写，确认
`aof_enabled=1`、`aof_rewrite_in_progress=0`、`aof_last_bgrewrite_status=ok`。
restore-verify 核验 schema、对象 SHA256、当前 version/pointer、manifest、双路
chunk ID/count/profile、Redis AOF/noeviction。历史无 profile 命名空间的 Dense
部署需按原运行配置进行复核，不能混用新 profile 数据。

演练最后人工执行新上传、旧公开文档查询/引用、跨租户404、撤权404、重试/死信/replay、
kill/redelivery；恢复器先运行、Worker 后启动，避免过期 broker 消息影响业务。
记录冻结起点、最后可恢复数据时间、每阶段耗时、错误、RPO/RTO 和人工确认。
1/4/8 小时恢复与 RPO 24 小时是目标，不是现有实测值。50 QPS/10万文档另做压测。

## 监控与回滚

`python -m scripts.infrastructure_status` 输出聚合任务状态、死信数、
outbox 未发送/最旧时间、过期租约、Redis 内存/淘汰/AOF 状态。
告警：死信增长、outbox 持续60秒、过期租约、协调 Redis eviction 非零、
AOF 写失败、磁盘空间、backup_age、restore演练失败。CLI 输出由部署监控采集，
不是已经部署的 Prometheus/告警平台。

回滚先停止新增异步受理，停止 dispatcher/Worker，保留账本、checkpoint 和新表；
不得直接删除 broker 或 downgrade 事实表。旧同步入口保留。对象切回本地前必须
复制并核验改造后新增对象，不能因目录曾保留就直接切换。DeepSeek 关闭 enabled
即可回到 LM Studio；不需要改检索索引。保留旧 generation/pointer 以供版本回滚。

## 验收结论

离线测试、真实隔离服务 smoke、托管真实验收三层独立。当前 MinIO 镜像拉取拒绝、
托管备份权限尚缺；DeepSeek 真实 key 已完成公开样本联调，但业务外发授权未确认。
生产身份、Linux prefork、
kill/redelivery、容量和完整四系统恢复演练未通过前，不通知“最终验收已就绪”。
