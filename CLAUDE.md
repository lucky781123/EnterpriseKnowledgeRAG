# AI 工程交接上下文

更新：2026-10-03。适用于Claude/Codex后续会话；以源码和最新验收产物为准。
先读 `PROJECT_CONTEXT.md`、`README.md`、本文件，再读相关 `.planning/` 和 `docs/`。

## 状态与范围

当前本地回答模型已按用户指定切换到`qwen3-coder-next-reap-48b-a3b-mlx`。
配置入口为`config/local-llm.toml`，由.env的LOCAL_LLM_CONFIG_FILE显式启用，
Settings启动时读取并优先于旧LLM/Embedding/BGE环境变量。修改TOML后stop/start，
不改代码、不热更新、不自动下载；API Key仍只读环境变量。缺失或错误文件拒绝启动。
model-registry.yaml不是运行配置。`[embedding]`配置model/base_url/dimension，
`[reranker]`配置model/base_url，均已从同一TOML读取。
当前Embedding为feadxus/qwen3-embedding:4b/2560维；8003的HTTP Jina替代进程内BGE。
换Embedding须先回填新profile：加载.env后运行
`python -m scripts.reindex_local_models --apply`（默认无--apply只计划），再重启受控API。
工具限本机显式development匿名身份的授权当前文档，不用于生产或任意租户。
9份文档/179个Child已回填，旧1024维索引保留；同一version新增profile不重复发布version。
公开样本新上传后10个scope，2560维持久化及Dense65/BM25F1/RRF60/Jina/回答引用通过。
股东题约9.25秒生成/stop，但引用failed/invalid_citations，尚未修复引用与来源问题。
全量371 passed/12 skipped，真实PG16 passed；最新证据
`artifacts/experiments/2026-10-03_embedding-reranker/README.md`。

前序未解决问题：“平安银行公司股东数量及持股情况”使原蒸馏回答模型重复推理，
2047个输出token均为reasoning，finish length。相同证据采样对照21.278秒/stop，
但引用failed，不能算通过；reasoning=off原生探测400，原因尚未核实。
5条证据来自2份文档，2条来源标题不含“平安”，Prompt只有内部ID没有标题；
存在来源识别风险，不能仅凭标题判断跨银行污染。建议对照已有
`qwen3.6-35b-a3b-turboquant-mlx`，此前诊断轮未切换；后续按用户要求改用上述coder模型。
诊断脚本`scripts/diagnose_answer_model.py`和
`artifacts/experiments/2026-10-03_answer-model/README.md`是本轮事实来源。

方案C本地P0已真实跑通上传到引用，并验证授权撤销和故障降级。
目标是可验证的学习/面试工程，不是已上线企业生产系统。
早期鉴权证据：`artifacts/experiments/2026-10-03_runtime-acceptance/`。
不要把历史2026-09-18“真实smoke待执行”误认为当前状态。

**更晚的追加验收**：`artifacts/experiments/2026-10-03_no-token-text-pdf/`。
实际本机 `P0_ANONYMOUS_ENABLED=true`，UI无Token输入；TXT与308279字节文本PDF
均由真实Chrome上传201，ready/published/active，真实本地回答answered/引用passed。
入口、Gradio预算和摄取注册均已修复，不能再让用户填写模型Token解决401/422/413。

## 启动

- 根目录 `bash start.sh` / `status` / `stop`，自动避让已有端口。
- 实际本轮API `127.0.0.1:8001`，前端 `/product-workbench/`。
- Python：`/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python`，3.11.15。
- 旧 `.venv` 3.9.6不适用；不要删除用户环境。
- 脚本加载 `.env`；`Settings.from_env()` 不自动读dotenv。
- LM Studio1234、Ollama11434、Jina Reranker8003、Weaviate8080、PostgreSQL54329。
- 仅管理自己的API PID；共享依赖和用户原8000 API保持不动。
- `.local/runtime/service.json` 记录进程，`api.log` 为本地日志。
- `.local/runtime/acceptance-token.txt` 仅历史鉴权验收使用；当前匿名UI不读取。
- 匿名仅development/loopback，固定local-demo/local-demo-user与editor/reader，
  ACL仍执行；默认false，不用于公网/多用户企业权限。UI queue和API都检查peer/Origin。
- 7860当前为307跳转兼容入口；只维护8001一套产品UI。
- .env变化须stop/start；普通JSON128KiB，产品多文件上传默认无项目层面的数量/大小上限，
  包括multipart文件本体、Gradio临时上传和queue/join批量元数据；由multipart解析器
  流式落盘。仍受磁盘、内存、网络、格式和解析器能力限制。

本轮Embedding `feadxus/qwen3-embedding:4b`/2560维；
Reranker `jina-reranker-v3-mlx`/HTTP `http://127.0.0.1:8003/v1/rerank`；
LLM `qwen3-coder-next-reap-48b-a3b-mlx`（`config/local-llm.toml`）。
`[embedding].dimension` 必须与真实输出一致，历史1024/4096不可混用。
Dense collection按tenant/profile隔离；旧profile指针与向量不删除。

## 代码地图

| 模块 | 文件/职责 |
|---|---|
| 装配 | `scripts/local_p0_api.py`：Database、Provider、索引、授权、Gradio |
| API | `api/app.py`、`product_schemas.py`：安全错误、deadline、引用复核 |
| 配置 | `config/local-llm.toml`、`config/settings.py`：三类模型文件优先、环境变量兼容与校验 |
| 数据 | `db/models.py`、`session.py`、`migrations.py`、`state.py` |
| 摄取 | `ingestion/{validation,parsers,chunking,service,product_service}.py` |
| 索引 | `retrieval/{indexer,generation,weaviate,weaviate_sparse}.py` |
| 查询 | `retrieval/{scope,product,hybrid,service,sparse}.py` |
| 回答 | `excerpts/{service,validation,protocols}.py` |
| 模型 | `providers/{lmstudio,registry,scheduler,errors,protocols}.py`、`embedding/ollama.py` |
| 排序 | `reranking/{http,bge,adapters,protocols}.py`，当前HTTP，BGE仅旧兼容路径 |
| 授权 | `security/{dev_token,context,authorization}.py` |
| 对象 | `storage/{local,protocols}.py` |
| 前端 | `workbench/app.py`：只调用HTTP，不直接访问DB/模型 |
| 评测 | `evaluation/`、`scripts/*evaluation.py`、历史 `artifacts/` |

Migration是Python contract0001–0004，通过
`python -m enterprise_rag.db.migration_cli` 执行，**不是Alembic**。
API readiness不自动迁移。PostgreSQL有11张业务表；新产品向量在Weaviate，
旧demo JSON embedding保留。连接池默认3+2，事务 `SET LOCAL` 超时。

## 真实数据流

1. `POST /api/v1/documents` 默认验证开发Bearer；本机显式匿名时固定演示context，
   不校验Token。两种模式均检查editor/admin、multipart白名单及全链路ACL。
2. 基础摄取按tenant/idempotency key协调，hash、同内容version复用与失败replay。
3. 自研UTF-8 TXT/Markdown/HTML/PyPDF2 → ParsedBlock → Parent800/Child320字符、无overlap。
4. 原文、request meta、parsed manifest写 `.local/p0-object-storage`；
   PostgreSQL保存job/version/artifact/ACL等权威事实。
5. Ollama Child embedding → Dense；字段投影 → Sparse。
6. 两路scope/profile/count/chunk identity/hash齐备才ready；
   PostgreSQL事务发布version/current pointer/generation。
   **两路共用一个双路generation/active pointer，不是独立切换。**
7. `POST /api/v1/rag/query`：可信context → ACL/current generation scopes →
   一次embedding → scope内顺序Dense/BM25F → RRF60 → HTTP Jina →
   Parent去重/授权 → F2-G → LM Studio JSON → identity/逐字引用校验 →
   出站前当前版本/ACL复核。

产品检索与最终ACL受同一总deadline，F2-G仅获剩余预算。
无scope/无证据不调用LLM，不允许绕ACL的纯模型常识兜底。
上传同步处理，没有经验证的“整体摄取固定60秒”承诺。

## 降级策略

| 故障 | 行为 |
|---|---|
| Embedding不可用 | Sparse-only，mode degraded |
| Dense不可用 | 保留Sparse；Sparse故障反之 |
| Reranker可恢复故障 | 保留RRF，reranker_used=false |
| 双路不可用 | 503，不调用LLM |
| 非retryable认证/协议/越权 | 停止，不借另一通道掩盖 |
| 无权限/无证据 | 404，不调用LLM |
| 本地LLM不可用 | 503，不伪造回答 |
| 总预算耗尽 | 504，取消等待，不重置预算 |

可降级网络类别为连接/超时、408/429/5xx；认证/请求4xx不可降级。
trace有mode/fallback_reasons和各阶段计数。当前不切换第二本地/在线模型；
成功降级仍调用同一真实本地回答模型。

## 入口区分

- `/api/v1/query`：caller-supplied evidence，F2-G JSON/SSE，不是授权检索。
- `/api/v1/retrieval/query`：旧PostgreSQL JSON + 应用cosine demo。
- `/api/v1/documents`、status/ACL、`/api/v1/rag/query`：本次产品路径。
- `scripts/gradio_enhanced.py` 为7860跳转兼容入口，不再实现第二套旧UI/摘录问答。
- `compose.p0.yaml` 从归档恢复；其余历史脚本保留，优先新 `start.sh`。

## 测试与证据

干净shell全量 `python -m pytest -q`：371 passed / 12 skipped；单独真实PG16 passed。
开发 `.env` 仅注入 `tests/integration` 做真实数据库验证。
HTTP smoke、DB/index核验、8个真实依赖ASGI故障注入、浏览器与替身测试分开记载。
最新TXT/PDF证据额外验证5个无Token故障用例；四个单依赖仍用真实LLM回答，
双路全部失败503、不调用模型。`scripts/verify_no_token.py`只允许公开样本。
历史 `local_p0_smoke` 需独立 `P0_ANONYMOUS_ENABLED=false` 服务，不能对当前匿名
服务测试跨tenant/撤权：忽略Token的固定身份不具备这种实验语义。

smoke必须断言published/active/artifacts ready、两路命中、RRF/配置的Reranker、
answered/引用passed、跨tenant与撤权404。

多文件最新证据在 `artifacts/experiments/2026-10-03_multi-upload/`：
真实Chrome一次选择3个文件，其中PDF为12583979字节；3个独立Document ID均发布并
ready，底层持久化和检索引用均通过。该实验全量回归为327 passed/12 skipped，
PostgreSQL集成为16 passed。
默认问答2048 tokens/300秒，UI立即显示处理中；incomplete和引用失败不得标passed。
最新真实“试用期”默认查询16.913秒，answered/引用passed（5条）/finish stop，
脱敏证据 `private-query-summary.json`；仍不是所有条文语义完整性的人工验收。
`python -m scripts.verify_local_runtime` 仅允许精确匹配公开 `_SMOKE_DOCUMENT` 的正文
进入 `answer.json`。故障函数只抛异常，其他依赖仍真实连接。
复现命令和最终数量以实验README为准。

新增脚本/测试局部Ruff通过；部分原有代码受本机新版规则影响，
不要声称全仓Ruff零告警，不为清理告警扩大重构。
Starlette/httpx与PyPDF2有弃用warning，本任务不升级。

## 边界与后续

- Sparse-only仅Child；BM25F GraphQL未返回全部locator，不承诺完整Parent详情。
- 多scope仍串行/N+1；通道内已按分数合成全局排名，容量与跨collection校准未验证。
- 两路完整性读取已修复REST忽略where的问题，cursor分页后精确核验scope；
  同租户多文档真实浏览器上传/查询已通过。当前分页会扫描tenant collection，
  不是大规模索引的性能方案。
- 当前HTTP Jina共享client、有界timeout和取消传播；不在adapter内重试或切换模型。
  客户端取消不证明服务端立即停止推理。旧BGE兼容路径为CPU4线程、串行准入与
  批次取消；生产资源隔离仍未实现。
- `.local/` 原文/token/日志和 `.env` 不提交。
- 未接入OIDC、Redis、异步Worker、OCR、Git同步、Agentic Rewrite、
  HA/备份恢复/SLO、在线Provider fallback。
- 40条评测草案和8条合成检索查询不能替代人工企业评测。
- LangChain/LangGraph仅为候选/边界约束，当前流程未使用。

实际Skill目录：`retrieval-pipeline-engineering`、`rag-degradation-and-fallback`、
`rag-acl-multitenancy-security`、`fastapi-model-provider-service`、
`deepeval-rag-evaluation`。Skill存在不代表生产能力已完成。

## 修改约束

工作树有用户大量staged/unstaged/untracked修改，不能回滚/删除。
只改任务必需代码，中文注释，保留稳定契约。
凭据本地读入进程，不输出.env、DSN、密码、token、私人正文或内部reasoning。
结论必须关联真实产物，不以HTTP200冒充answered。
