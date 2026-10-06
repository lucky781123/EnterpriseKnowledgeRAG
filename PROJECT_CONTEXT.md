<!-- 项目入口：新会话优先读取本文件，再按任务读取 docs/ 下的专题文档。 -->
# EnterpriseKnowledgeRAG 项目上下文

## 1. 项目目标

从零开发一个面向研发技术文档的企业级 RAG 知识库，用于高级大模型开发岗面试。目标不是冒充真实生产平台，而是完成：

- **P0：** 可运行的端到端闭环；
- **P1：** 检索、权限、可靠性、性能等关键能力有可复现实验证据；
- **P2：** 有清晰、带触发条件和代价说明的生产演进方案。

最终交付目标：**P0 完整 + P1 关键证据 + P2 可演进**。

## 2. 当前状态（2026-10-03）

### 2026-10-06 回答生成预算8192（最新）

- 已确认：用户报告UI达到长度上限，要求最多8192；此为回答LLM输出预算，
  不能取Embedding/Reranker输入上限。未修改切块、向量维度、模型或外发策略。
- 已实现：产品API/UI默认及上限8192，共用Excerpt/旧PG请求上限8192，
  旧入口默认512不变；300秒deadline与length→incomplete校验不变。
- 已验证：默认/显式8192传到Provider，8193调用前422；全量477 passed/12 skipped/
  10 warnings，工作台与新增测试Ruff通过。schema六条既有style告警在HEAD确认，
  未顺带修复。真实PG集成本轮未重跑。
- 已重启本项目API8001并刷新UI，Worker/dispatcher仍运行；真实公开HTTP与
  浏览器8192默认问答answered/引用passed/stop。截图.local/runtime/max-tokens-8192.png。
  证据artifacts/experiments/2026-10-06_output-budget/verification.json。
- 待验证：用户原长问题未复跑，不宣称全部长回答/实际输出8192tokens已通过；
  增加预算不等于重复生成、引用或托管验收问题已修复。

### 2026-10-06 本机异步后台/UI 启动验收（最新）

- 用户明确要求启动后台和UI。已新增`bash start.sh start-async/async-status/async-stop`，
  新建独立双Redis7.4持久实例、显式0005迁移、Celery macOS solo/dispatcher、
  重启登记归属API8001。PG/Weaviate/Ollama/Jina/LM Studio健康，Worker ping通过。
- 已补UI异步入口：提交入库返回202/Task ID，查询阶段，仅succeeded完成、死信失败；
  原同步API与旧Document ID查询保留。UI仅infra启用后切换，默认不变。
- 已验证真实运行公开样本受理0.047秒、六阶段发布28.387秒，
  HTTP与浏览器问答answered/引用passed。API/UI与后台留运行供用户验收。
  截图`.local/runtime/async-ui-acceptance.png`，状态`service.json/background.json`。
- 已验证全量472 passed/12 skipped/6 warnings，定向33项与局部Ruff/compileall/diff通过；
  本轮未重跑真实PG integration；前轮隔离16项结果保留为历史证据。
  归档`artifacts/experiments/2026-10-06_local-async-ui/verification.json`。
- 边界：storage local、development匿名/loopback、macOS solo；
  没有托管MinIO配置，业务allowlist仍空，UI走真实LM Studio；不外发私人文档。
  入库Ollama仅本机复用，不是生产资源隔离。托管恢复/生产身份/容量仍待验收。

### 2026-10-06 DeepSeek 真实公开样本联调（最新）

- 已确认用户填入真实 key；`config/deepseek.toml` 为 enabled=true/空 allowlist。
  未打印 key、不读 `.env`，未修改业务 allowlist、运行服务、数据库或已有文档。
- 已实现 `scripts/deepseek_smoke.py`：默认计划，显式 execute 才访问模型，
  仅源码固定公开合成证据；进程内临时放行 `deepseek-public-smoke`。
- 已验证真实 DeepSeek 非流式3.097秒、SSE3.467秒，均 answered/引用passed/stop；
  注入HTTP503后真实LM Studio非流式/流式均仅兜底一次并通过引用；
  未允许tenant/无context均在线调用0。不是完整RAG在线验收。
- 已实现专用公开身份Worker和在线完整隔离演练：202→Celery六阶段→发布→
  混合检索/Jina→DeepSeek answered/引用passed，在线1/兜底0；跨tenant404、
  撤权后404且在线次数不增、对象hash/pointer通过。补守卫后复跑通过。
  使用local对象/macOS solo，临时容器已关闭，不代表托管或生产验收。
- 已验证最终离线全量462 passed/12 skipped/6 warnings，真实隔离PG集成16 passed；
  局部Ruff/compileall/diff通过。
  证据 `artifacts/experiments/2026-10-06_deepseek-public-smoke/verification.json`。
- 待实验/阻塞：业务租户外发授权、托管备份恢复权限、
  Linux prefork kill/redelivery、生产OIDC、50QPS、RPO/RTO。真实key不再是阻塞。
- 非目标：不把 `local-demo` 假定为纯公开数据，不先外发私人文档再过滤输出；
  空业务allowlist仍只走本地。最终托管验收尚未完成。

### 2026-10-06 异步业务装配与真实隔离闭环（最新）

- 已实现异步 202 受理、任务状态/replay、Celery opaque event 消费、outbox/recovery、
  parse/Embedding/Dense/Sparse/verify/publish 六阶段、PG checkpoint/fence 原子发布。
  原同步 API 保留，新能力默认关闭；Worker 必须重载身份而非使用角色快照。
- 已实现 Redis tenant cache/锁/限流接入、全写入 PG 门禁、原文迁移工具、
  四系统 backup/restore/restore-verify CLI；恢复只允许空隔离目标。
- 已实现 DeepSeek 主用/LM Studio 单次兜底，deadline 继承、首事件后不切换，
  在线外发前和回答出站前复核全部资源 ACL。配置 `config/deepseek.toml`，
  key 填 `.local/secrets/deepseek-api-key.txt`；默认关闭、空 allowlist 拒绝外发。
- 已验证隔离 PostgreSQL16/双 Redis7.4/Weaviate1.27.10 + 真实 Ollama/Jina/LM Studio
  的公开合成文档 202→Celery 六阶段→发布→检索问答；
  `answered/citation passed`、跨租户404、原文/manifest/pointer 校验通过。
  本次用 local storage/macOS solo，不代表托管 MinIO 或 Linux prefork 验收。
- 离线全量459 passed/12 skipped，隔离真实PG集成16 passed；局部Ruff、compileall、
  diff检查通过。新增运行与恢复说明 `docs/architecture/infrastructure-runbook.md`。
- 阻塞/待实验：真实DeepSeek key/外发allowlist、托管MinIO与Weaviate backup权限、
  Linux prefork kill/redelivery、四系统恢复演练、RPO/RTO、50QPS和生产OIDC。
  MinIO镜像当前拉取被registry拒绝，未把SDK替身或备份完整性测试冒充真实恢复。
- 当前结论：此次主要代码已实现、公开隔离闭环通过，**最终真实托管环境验收未完成**。

### 2026-10-06 基础设施演进（最新追加，尚未完整装配）

- 用户已批准推荐方案：MinIO/Weaviate/DeepSeek 托管，Redis/Celery/PostgreSQL 自建；
  Celery 使用 Redis broker/backend，不引入 RabbitMQ，缓存和协调使用独立 Redis 实例。
  此次决策覆盖第 3 节旧 Celery+RabbitMQ 规划；共享单机不宣称主机级 HA。
- 已新增基础配置、Redis 缓存/锁/限流与 tenant/profile Embedding wrapper、
  MinIO ObjectStorage adapter 和 Celery 配置工厂。默认关闭，旧 factory/API 未切换。
- 已提供显式 `0005_async_ingestion` migration 与 TaskLedger/OutboxDispatcher：
  任务/stage/outbox 同事务、tenant/owner 可见性、最多额外重试 3 次、
  PG 死信/人工 replay、租约/fence/checkpoint、过期阶段和丢消息恢复。
  默认 migration/CLI 仍为 0004，未应用到真实库。
- 已验证离线全量 437 passed/12 skipped；基础组件/账本 59 项、连同旧迁移定向 70 项；
  局部 Ruff、compileall、diff 检查通过。12 项缺 DSN 跳过，未跑真实 Redis/MinIO/Celery/PG。
- 待实现真实阶段 Worker、异步受理/状态/replay API、流式原文与迁移、
  全业务写入门禁、四系统备份/恢复脚本、DeepSeek 外发政策/单次本地兜底、
  生产身份、Compose/监控装配；50 QPS 与 RPO/RTO 仍待实验。
- DeepSeek endpoint/key/model 留空；未读取 `.env`、未改现有模型/容器/业务对象。
  历史股东题引用失败与失败 message 出站 ACL 风险没有因新增基础组件而修复。
- 架构与迁移/恢复/回滚见 `docs/architecture/infrastructure-evolution.md`；
  激活计划 `.planning/2026-10-06-infrastructure/`，下一步为阶段 2 真实业务装配。

### 最新：三类模型统一配置与Embedding / Jina切换

- 运行配置唯一入口为`config/local-llm.toml`，`.env`通过`LOCAL_LLM_CONFIG_FILE`
  启用。顶层为回答LLM；`[embedding]`包含model/base_url/dimension；
  `[reranker]`包含model/base_url。文件优先于旧环境变量，错误配置拒绝启动。
- 当前LLM为`qwen3-coder-next-reap-48b-a3b-mlx`（LM Studio1234）；
  Embedding为`feadxus/qwen3-embedding:4b`（Ollama11434，实测2560维）；
  Reranker为`jina-reranker-v3-mlx`（8003的`POST /v1/rerank`）。
  API进程不再加载BGE；未启用嵌套配置的旧部署保留兼容路径。
- 修改文件后stop/start，不热更新、不自动下载/加载模型。仅换LLM/Reranker无需回填；
  换Embedding须匹配实际维度，并执行`python -m scripts.reindex_local_models --apply`。
  命令需先加载`.env`，详细顺序见README；工具限本机development演示身份授权的文档。
- Dense物理collection按profile隔离，旧1024维索引和旧指针保留；同一当前已发布version
  允许新增profile，只发布新generation pointer，不重复改变version发布时间。
- 已回填9份授权文档/179个Child；真实公开TXT新上传201后新profile为10个scope。
  DB/原文/manifest hash、lineage、双路对象、2560维、published/active/ready均核验通过。
- 最新真实查询Dense65/Sparse1、RRF60/Jina正常、answered/引用passed/stop；
  公开回答为“MODEL_SWITCH_CHECK 审批期限为三个工作日。”。
  股东题生成约9.25秒/stop，但仍invalid_citations/failed，来源与引用问题未修复。
- 全量371 passed/12 skipped，单独真实PostgreSQL16 passed；新增配置/HTTP契约/
  profile隔离/发布与维度守卫测试通过。新增模块和相关文件Ruff通过；
  原检索模块3条既有告警不扩大修复。证据：
  `artifacts/experiments/2026-10-03_embedding-reranker/README.md`。

### 前序：本地回答模型配置文件与用户指定模型切换

- 本机`.env`显式指定`LOCAL_LLM_CONFIG_FILE=config/local-llm.toml`；Settings启动时
  读取TOML的model/base_url/max_concurrency/max_queue_size，文件优先于旧同名环境
  配置。显式文件缺失、非法TOML、错误字段/类型拒绝启动，不静默回退旧模型。
- 用户修改配置后`bash start.sh stop`再`bash start.sh`即可，不需要改代码；
  不实现热更新/自动下载。密钥仍由LOCAL_LLM_API_KEY注入；该历史轮Embedding/Reranker不变。
  未启用文件的旧部署仍读环境变量，model-registry.yaml只作能力和历史记录。
- 按用户要求切换到LM Studio已加载的`qwen3-coder-next-reap-48b-a3b-mlx`。
  重启仅受控8001 API，其他API、数据库、模型服务不停止；API/frontend200。
- 真实公开摘录问答2.971秒，返回模型ID与TOML一致，answered/引用passed/stop。
  股东产品查询16.233秒、stop/输出437，引用仍failed/invalid_citations，
  故该问题尚未通过业务验收，不把模型切换等同于来源与引用问题修复。
- 新配置测试12项；全量339 passed/12 skipped，本轮未重跑真实PG集成。
  脱敏证据`artifacts/experiments/2026-10-03_local-model-config/verification.json`。

### 前序诊断：原蒸馏模型股东问答重复推理

- 原蒸馏回答模型在用户这道题上输出2047 tokens，均计为reasoning并重复，
  finish length；不是证明Embedding/Reranker或服务代码无限循环。
- 真实factory取得5条授权证据，来自2份文档；2条来源标题不含“平安”。
  模型输入仅内部document/chunk ID，没有标题；存在主体归属风险，但尚未确认
  标题不匹配的文档一定属于另一银行。
- 相同证据有界采样对照21.278秒、finish stop、长行无重复，
  但invalid_citations/failed，不能算业务成功。原生reasoning=off探测400，
  未核实拒绝原因，未证明开关有效。
- 建议先对照本机已有`qwen3.6-35b-a3b-turboquant-mlx`，再考虑
  `qwen3.6-27b-oq8-mtp`；两者尚未生成验收。不擅自卸载共享模型。
- 新增`scripts/diagnose_answer_model.py`及脱敏报告
  `artifacts/experiments/2026-10-03_answer-model/README.md`。
  此前诊断轮未改8001、`.env`与线上Provider；本轮切换状态见最新节。
  后续须验证模型引用/主体/日期，
  并处理来源标题缺失，不能用提高token或宽松解析冒充修复。

### 最新追加：免Token TXT / 大PDF

- 当前本机 `.env` 显式 `P0_ANONYMOUS_ENABLED=true`；UI Token控件隐藏，
  上传/状态/问答不校验Token，不读取短期token文件；不涉及模型密钥。
- 服务端固定 `local-demo/local-demo-user`，editor/reader，ACL仍执行。
  仅development/真实loopback peer/本机Origin，Gradio queue也受保护；
  Uvicorn禁用proxy headers，默认配置仍关闭匿名。
- UTF-8 TXT支持校验、MIME、保真段落parser和服务注册；
  UI空source/title/idempotency自动补齐。FAILED仍需显式replay。
- Gradio临时文件路径使用流式磁盘multipart上传，工作台支持多文件；
  产品上传默认不设项目层面的数量/大小上限，普通JSON仍128KiB。
  单文件失败不阻断同批其他文档，Source/幂等键按文件独立生成。
- 真实Chrome两次上传201/发布：中文TXT与308279字节含图文本PDF，
  无Token和元数据留空，DB/对象/双路/1024维/hash/lineage/pointer均通过；
  实际回答answered、引用passed，Dense/BM25F/RRF60/BGE均命中。
- 4种单依赖故障降级真实本地LLM各1次；双路全部故障503/调用0次。
  全量303 passed/12 skipped，真实PostgreSQL16 passed。
- 最新证据 `artifacts/experiments/2026-10-03_no-token-text-pdf/README.md`；
  旧runtime-acceptance是默认鉴权实验，不能替代此次TXT/PDF结论。
- API/UI保持8001运行；7860兼容入口只307跳转到统一工作台，
  用户原8000与外部模型/数据库不动。改.env需stop/start。

### 最新追加：多文件与无上传数量/大小上限（2026-10-03）

- 旧UI的 `gr.File` 未设置 `file_count="multiple"`，Gradio标准上传器还固定
  `max_files=1000`，挂载参数曾固定 `max_file_size=10MiB`；这是此前只能选一个
  文件和大PDF返回413的直接原因。
- 现在工作台支持多选；每个文件按文件名生成独立 `source_id`/幂等键并逐个调用
  产品API，单文件失败不会阻断同批其他文档，结果和Document ID逐项回填。
- 产品 multipart、Gradio流式临时上传和工作台 `queue/join` 批量元数据均不设
  项目层面的数量/大小上限；普通API JSON仍保持128 KiB。这里的“无上限”不等于
  物理无限，仍受磁盘、内存、网络、multipart格式、解析器和下游模型能力限制。
- 修复多scope检索按文档遍历顺序截断候选的问题：Dense/BM25F结果先按通道
  分数形成全局排名，再做RRF和候选截断，后上传但高相关的文档不会被旧文档挤掉。
- 真实Chrome一次上传 `alpha.txt`、`beta.md` 和 12583979 字节 `large-policy.pdf`，
  3个独立文档均201/published/active/ready；DB、原文/manifest hash、切片lineage、
  双路索引、1024维向量、RRF/BGE、本地回答和逐字引用均通过。
- 证据目录：`artifacts/experiments/2026-10-03_multi-upload/`；
  全量离线回归327 passed/12 skipped，真实PostgreSQL集成16 passed。
- 产品/UI默认问答预算已从512提高到2048 tokens、300秒，立即显示处理中。
  HTTP200的incomplete/引用失败不再标passed。最终真实“试用期都有哪些条文”
  默认查询16.913秒、answered、5条引用passed、finish stop；仅保存脱敏状态在
  `private-query-summary.json`，不证明所有相关条文均已覆盖。
- BGE默认CPU4线程、串行准入，取消请求设置Event并在批次边界停止；当前原生kernel
  不能强制打断，worker结束前不释放占位。WindowServer排查按用户要求停止扩展。

### 本轮早期鉴权验收（保留追溯）

本轮已完成真实本地P0上传到引用验收，历史2026-09-18配置阻塞已解除。
新会话以最上方三模型最新节、`CLAUDE.md`及该节证据为准；下列早期验收参考
`artifacts/experiments/2026-10-03_runtime-acceptance/README.md` 为最新事实。

- 一键 `bash start.sh`，支持status/stop；API `127.0.0.1:8001`，
  Gradio `/product-workbench/`。原8000服务保留；日志/状态 `.local/runtime/`。
- Python为Conda `enterprise-rag` 3.11.15；旧 `.venv` 3.9.6缺依赖，不用/不删。
  实际Gradio6.29.0。
- 实际模型：Ollama `bge-m3:latest`/1024维，BGE `BAAI/bge-reranker-v2-m3`/CPU，
  LM Studio使用下述历史长模型名；Weaviate1.27.10、PostgreSQL16。
- 首次上传因入口历史写死4096维返回503，已改为显式
  `OLLAMA_EMBEDDING_DIMENSION`（本地1024），模型/维度均进入profile，守卫保留。
- 正常HTTP上传201，job succeeded、version published、generation active，
  四类artifact ready；双路/RRF/BGE均命中，模型answered、引用passed。
- DB/对象/索引原文hash、manifest hash、lineage、active pointer、
  两路chunk IDs/count及实际1024维均交叉核验通过。
- 8类故障用例通过：embedding/dense/sparse/reranker降级仍用授权证据调用真实LM；
  双路故障/认证/无权限/模型不可用明确失败。
  故障注入为真实factory的ASGI HTTP；正常路径为运行中服务网络HTTP，分开记载。
- 跨tenant/撤权404，重授200/answered/引用passed，不做绕权限纯模型兜底。
- 干净全量279 passed、12 skipped；真实PostgreSQL集成16 passed。
- 真实浏览器上传/状态/问答/SSE完整trace通过，桌面/移动视口无横向溢出；
  同租户多文档发布已验证。修复Dense/Sparse REST列表不执行where导致其他
  generation污染完整性计数的问题，新增cursor分页和scope/profile核验。
- 仅恢复归档中的 `compose.p0.yaml`，其余旧脚本不用，新建受控 `start.sh`。
- 本轮规划 `.planning/2026-10-03-runtime-acceptance/`，交接
  `.planning/handoff/HANDOFF.md` 最新节；保留前序修改。

### 2.1 实际架构与边界

- 三条API：caller-supplied摘录、旧PG cosine demo、产品Weaviate混合检索。
  验收必须用 `scripts.local_p0_api:app --factory` 的产品入口。
- 自研TXT/Markdown/HTML/PyPDF2；Parent800/Child320 **字符**、无overlap，
  当前未用LangChain/LangGraph做解析/在线流程。
- PostgreSQL11表为权威事实；原文/manifest本地对象存储，向量/词法Weaviate。
  Migration为Python contract0001–0004，不是Alembic。
- 两路共用generation/active pointer，双路完整性齐备后事务发布，
  不是独立pointer或跨系统分布式事务。
- scope内顺序Dense/BM25F、RRF60、HTTP Jina、Parent去重/ACL、LM、出站ACL复核。
  检索和出站复核共享deadline，F2-G仅获得剩余预算。
- Sparse-only使用Child正文，部分locator为空；多scope串行/N+1、
  HTTP取消不保证上游推理即时停止均为生产边界。BGE取消限制仅适用旧兼容路径。
- OIDC、Redis、Celery/RabbitMQ、Git同步、OCR、Agentic、在线Provider fallback、
  HA、容量/SLO、备份恢复未接入或未验收。Skill/设计图不等于实现。
- `.env` 和 `.local/` 不提交；历史鉴权验收token仍位于
  `.local/runtime/acceptance-token.txt`，当前匿名UI不使用它，不在聊天里输出。

## 2H. 历史状态（2026-09-18，保留追溯）

- 业务 RAG 的方案 B 最小闭环已实现并取得真实依赖 smoke 证据；完整生产级 RAG 仍未完成。Phase 3 的 PostgreSQL 基础切片和真实摄取基础已实现，覆盖数据模型、异步 session、migration contract、状态机、受控解析、确定性 chunk lineage 和版本状态编排。
- 本机：MacBook Pro，M3 Max，128 GB 统一内存，2 TB SSD。
- 本地 Embedding：已确认 Ollama 模型 `hardcore_aryabhata_598/qwen3-embedding:8b` 可运行并输出正常。
- Reranker：已在工程内提供同步 `BGEReranker` 实现；通过 `SyncRerankerAdapter` 暴露统一异步 Provider，不再依赖 Ollama Reranker 服务。
- 本地 LLM：已在 LM Studio 部署多个模型；首期默认模型固定为 `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`，服务根地址为 `http://127.0.0.1:1234`，Provider 适配层负责调用 OpenAI-compatible 的 `/v1` 路径。
- 在线 LLM：已具备 Grok-4.6、GPT5.6-terra 和其他 GPT 模型的测试条件，具体可用性以实施时契约测试为准。
- 模型策略：本地优先开发、回归和压测；在线模型用于小规模质量对照和真实性能抽样。
- `omlx`：小模型 Router/Grade/Rewrite 的候选运行时，不是前置依赖；必须通过协议、并发和质量基准后再决定。
- PDF：首期受控支持文本型、结构相对简单的样本；不承诺复杂 OCR、复杂表格和版面完全还原。
- 评测集草案：`docs/evaluation/dataset-draft.jsonl`，共 40 条，等待人工抽样确认。
- 第 1 轮基础代码已完成首批骨架：配置、Provider 契约、Fake LLM、并发调度器、本地对象存储、领域实体和最小 API 已实现。
- 项目级 RAG Skill suite 已在 `.agents/skills/` 完成，包含 5 个 Skill、7 个共享 JSON Schema、10 个场景文件和 40 个压力场景；CI 和测试均以该目录为唯一权威来源。
- 历史 PostgreSQL/F2-G 基线曾记录离线 `126 passed, 11 skipped` 和有效开发 DSN `137 passed`；BM25F 中文对照阶段曾记录离线 `200 passed, 11 skipped`；当前 Task 8 最新离线代码级回归为 `261 passed, 12 skipped`。本轮未注入 `POSTGRES_DSN`，因此 PostgreSQL 集成仍明确跳过；该结果是离线回归，不是真实依赖 smoke 证据；新增入口/runner 的局部 Ruff、compileall 和 diff 检查通过。
- 环境预检：`docs/preflight-checklist.md`；用户已验证 Docker Desktop 正常（`linux/aarch64`、16 CPU、15.6 GiB），Minikube 未安装，`kubectl` 已安装。Codex 沙箱因 socket 权限可能无法复跑 Docker API。
- 本轮真实验证使用固定镜像 `postgres:16`，容器内 PostgreSQL 版本为 `16.15`，Docker Server 为 `linux/arm64`；测试结果仅证明当前 schema、事务和发布锁场景，不代表生产容量或高并发性能。
- PostgreSQL 连接池已按单机/CI 保守参数设计：`pool_size=3`、`max_overflow=2`、`pool_timeout=10s`、`pool_recycle=1800s`、`pool_pre_ping=True`；事务超时使用 `SET LOCAL`，默认 `statement_timeout=30s`、`lock_timeout=5s`。
- migration 由 Compose/部署 one-shot init container 执行；API `/health/ready` 只检查连接和目标 schema/migration 版本，不自动迁移；CI 已增加 PostgreSQL migration/integration job 配置。
- PostgreSQL 集成测试继续使用同一开发容器，但每个测试创建独立临时数据库并在结束后删除，不再清理固定库的 `public` schema。
- Compose 已完成动态验证：one-shot migration 成功时 API 按 `service_completed_successfully` 启动并通过 `/health/ready`，不可达数据库时 migration 非零退出且 API 不启动；验证使用临时 Docker 配置目录绕过用户级 credential helper 拉取问题，不修改用户级配置。
- Phase 3 PostgreSQL 基线、F2-G/F2-I 服务切片和真实摄取基础均已完成当前验收；下一步单独设计 Embedding、稀疏索引、可重建发布和检索闭环，不提前扩展 Agentic 范围。
- F2-G 原文摘录问答已实现：请求接收调用方提供的 `question + evidence`，执行 evidence identity 校验，固定记录 `context_source=caller_supplied`，并明确不把调用方上下文冒充为检索或授权结果。
- F2-G Provider/API 已实现：LM Studio OpenAI-compatible adapter、JSON complete、统一 SSE stream、模型 readiness、共享 `httpx` client 生命周期、按 alias 的 `LocalLLMScheduler`、队列/总 deadline、错误归一化、主动取消和清理路径均有对应测试。
- F2-G 评测与实验已实现：摘录 evaluator、Fake/mock/真实 LM Studio 分层证据、可复现 benchmark runner、并发 1/2、`max_tokens=128/256`、usage 缺失和取消清理结果均已写入 `artifacts/experiments/2026-09-14_f2-g/`；精简 F2-I 本地工作台已接入同一服务入口。
- F2-G 专项和真实摄取基础的历史证据保持不变；当前 Task 8 最新离线全量为 `261 passed, 12 skipped`。该结果是离线回归，不是真实依赖 smoke 证据；真实 LM Studio/F2-G 观测仍按原实验产物解释，不外推为生产质量或容量。
- F2-G 当前边界仍明确：F2-G 入口本身仍是 caller-supplied evidence 的摘录问答，不代表检索、授权或事实真实性；方案 C 产品入口已另行接入 Dense+BM25F+RRF+BGE、开发身份 ACL 和引用复核。真实本地 benchmark 不代表语义质量、成本、SLO、GPU 行为或生产吞吐。
- 真实摄取基础已实现：支持 Markdown、HTML 和受控文本型 PDF 的校验与统一解析，生成带标题路径/locator 的 `ParsedBlock` 以及确定性 Parent/Child chunk；原文和 parsed manifest 写入本地对象存储，PostgreSQL 保存 job/version/artifact 状态。
- 摄取可靠性已验证：同租户幂等键复用、内容冲突、FAILED 显式 replay、同内容 version 复用和共享 version 并发成功/失败均有测试；真实 PostgreSQL 并发失败证据证明 parser 只执行一次。摄取成功不会自动设置 `documents.current_version_id`。
- 真实摄取基础边界：该独立基础切片不包含 Git 同步、OCR、队列 Worker 或完整生产异步链；方案 C 产品上传会复用其受控解析能力，并在同步请求内继续执行后续索引与发布。parsed manifest 本身仍不是检索索引。
- 方案 B 最小检索闭环已通过真实 smoke：固定 Markdown 经 Ollama Embedding 写入 PostgreSQL JSON，应用层执行 cosine，按 tenant/version 过滤并回溯 Parent，再通过 FastAPI 调用 LM Studio；证据见 `artifacts/experiments/2026-09-16_retrieval-smoke/`。
- 方案 B 当前边界：旧 `/api/v1/retrieval/query` 仍是 PostgreSQL JSON + 应用层 cosine 的演示路径；方案 C 产品入口另行使用 Weaviate Dense/BM25F、RRF、BGE 和开发身份 ACL。两条路径都不代表生产容量、OIDC 或高可用证据。
- Weaviate Dense 迁移切片已实现：每个 tenant 独立 collection、generation scope、PostgreSQL active/previous pointer、批量 checkpoint、完整性校验、回填、retire、cleanup 和显式 repository 注入；默认 API 读取路径仍保持 PostgreSQL。
- Weaviate Dense 已在 Compose `1.27.10` 取得真实 transport smoke：collection、批量写入、完整性、nearVector 查询和删除均通过；GraphQL `limit` variable 与 minimal 删除计数的 1.27 兼容差异已修复。PostgreSQL active pointer 发布/回滚未纳入本次 transport-only smoke，默认读取路径仍未切换。
- BM25F Spike 已实现确定性字段投影和最小真实 transport：`title`、`section_title`、`api_symbols`、`body` 带权查询在真实 Weaviate 命中，错误 tenant/version 均返回 0；方案 C 已在本地产品服务中复用该 transport，但通用 Sparse Generation 状态机和生产级发布编排仍未冻结。证据见 `artifacts/experiments/2026-09-17_weaviate-real-smoke/`。
- BM25F/RRF 固定小数据集质量实验已完成：真实 Ollama `qwen3-embedding:8b` + Weaviate `1.27.10` 上，10 个合成 chunk、8 条查询的 Dense @3 Recall/MRR/nDCG 为 `0.9375/1.0/0.9516`，BM25F 为 `0.625/0.625/0.625`，RRF `k=20/60/100` 均为 `1.0/1.0/1.0`；Dense 独有命中 3 条语义改写，BM25F 在双相关查询中补足一个 Dense rank 4 证据。证据见 `artifacts/experiments/2026-09-17_bm25f-rrf-quality/`。
- BM25F 中文 tokenizer/预处理对照已完成：保持 Dense、字段权重、candidate K 和 RRF 不变，`word + identity` BM25F @3=`0.625/0.625/0.625`，`trigram + identity`=`0.75/0.75/0.75`，`word + cjk_bigram`=`1.0/1.0/1.0`；对应产物见 `artifacts/experiments/2026-09-17_bm25f-chinese-tokenization/`。
- 中文对照只证明当前 10 个合成 chunk、8 条查询上的字符重叠收益：`trigram` 仍漏掉 2 条语义改写并引入低分噪声，`cjk_bigram` 满分可能过拟合。生产默认 profile、人工确认 development/frozen 数据集、索引体积、容量和 SLO 仍待实验。
- 方案 C 本地 P0 产品闭环已实现：同步上传、开发 HMAC Bearer token、文档状态/ACL 管理、Dense+BM25F+RRF+BGE 查询和引用前复核均有离线契约证据；真实上传到引用 smoke 尚未在专用 PostgreSQL/Weaviate/Ollama/BGE/LM Studio 环境完成。

## 3. 已确认技术边界（含规划，实施状态以第2节为准）

- `Python + FastAPI`；内部调试使用 Gradio；REST + 统一 SSE。
- Docker Compose 开发；Minikube 做本地 Kubernetes 验证，云上迁移作为 P2 演进方案。
- PostgreSQL：业务元数据、租户、ACL、文档版本、任务状态、审计、Checkpoint。
- 对象存储：保存原文和解析产物；具体实现待实施前重新确认，不把 MinIO 视为无条件默认。
- Weaviate：向量与 BM25F 分别调用；应用自研双路编排、RRF、工程内 BGE Reranker Adapter。
- Celery + RabbitMQ：异步摄取与后台任务；Redis：缓存、限流、锁和短期状态，不作为唯一长期事实来源。
- Keycloak + OIDC；多租户、RBAC、文档级 ACL；召回前过滤，返回/Parent 回溯/缓存/引用前再次复核。
- `LangChain` 仅限 Parser/Loader/Splitter 文本处理适配层；不使用其 Retriever、VectorStore、LCEL、Agent、Memory 或业务对象。
- Linear 主路径；Agentic 仅作有界自纠错路径，最多一次 Rewrite + Retrieve，必须有 deadline、预算和降级。
- SSE 只输出模板化 `stage` 状态、`tool_call`（仅真实工具调用时）、`token`、`citation`、`error`、`done`，不输出真实内部推理链。

## 4. 不可擅自改变的原则

1. 先讨论和验证，再扩大范围；不要把设计假设写成已完成事实。
2. 只访问和修改当前项目目录；不访问桌面、其他 App 或项目外文件。
3. 所有重要技术选择都记录候选、理由、代价、验证方法和重审条件。
4. 任何评测结论必须关联代码、模型、Prompt、索引和数据版本。
5. 不以单机 Minikube、合成数据或替身模型冒充生产容量和真实企业效果。

## 5. 推荐启动提示

新会话可使用：

```text
请先读取当前项目目录中的 PROJECT_CONTEXT.md，再按任务读取相关 docs 文档。
只访问当前项目目录。严格区分已确认、已实现、已验证、待实验和非目标。
本轮先报告你理解的当前状态和下一步，不要擅自扩大范围。
```

## 6. 导航

- 技术栈：`docs/technology-stack.md`
- 系统架构与边界：`docs/architecture.md`
- 开发路线：`docs/roadmap.md`
- 验收矩阵：`docs/acceptance-matrix.md`
- 风险登记：`docs/risk-register.md`
- 评测规范：`docs/evaluation/evaluation-plan.md`
- 面试证据：`docs/interview/evidence-and-questions.md`
- ADR 规范：`docs/adr/README.md`
- 实验产物：`artifacts/experiments/`
- 压测产物：`artifacts/benchmarks/`
- 故障产物：`artifacts/failures/`
- 环境变量模板：`.env.example`
- 模型注册表：`config/model-registry.yaml`
- 项目级 RAG Skill：`.agents/skills/`
