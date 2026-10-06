# EnterpriseKnowledgeRAG

异步入库、Redis/Celery、DeepSeek 主备和备份恢复的迁移、启动、验证及回滚：
[基础设施操作手册](docs/architecture/infrastructure-runbook.md)。

面向研发技术文档的企业级 RAG 学习与验证工程。

**当前状态：2026-10-03 已完成真实本地 P0 闭环验收。** 上传、解析、
Parent/Child、PostgreSQL、Weaviate Dense/BM25F、RRF、HTTP Jina Reranker、
LM Studio 回答、引用和 ACL 已连通。单依赖故障可使用剩余授权证据降级；
无证据、无权限或模型不可用时明确失败。这不等于生产部署、容量或企业质量验收。

**追加验收：TXT / 308279 字节文本型 PDF 已在免 Token UI 真实上传并回答成功。**
本机 `.env` 已启用 `P0_ANONYMOUS_ENABLED=true`，不用填任何模型或开发 Token；
Source ID、标题、幂等键可留空。工作台支持一次选择多个文档；产品 multipart、
Gradio临时文件和批量 queue 元数据默认不设置项目层面的文档数量和大小上限，
普通 JSON 请求仍为 128 KiB。实际仍受磁盘、内存、网络、文件格式和解析器能力
限制。最新证据：`artifacts/experiments/2026-10-03_multi-upload/README.md`。

## 一键启动

在当前已配置的开发机执行：

```bash
bash start.sh
bash start.sh status
# 只停止该脚本管理的 API；保留外部模型、数据库和用户其他服务
bash start.sh stop
```

异步后台与 UI 本机验收入口：

```bash
bash start.sh start-async
bash start.sh async-status
bash start.sh async-stop
```

`start-async` 复用当前 `.env` 配置；缺双 Redis 时只新增项目独立 Redis7.4 实例，
不借用其他项目6379，持久卷保留。显式新增0005任务表、启动Celery Worker/dispatcher，
重启自己的API/UI后等待readiness与Worker ping。本机macOS用solo，Linux用prefork。
默认使用现有本地对象存储，未配置托管MinIO则不宣称其已接入；
入库Embedding未单独配置时复用当前Ollama，仅适用于本机验收。
异步UI点击“提交入库”后返回Task ID，点“查询状态”，`succeeded` 后再问答。
旧Document ID仍可查文档状态；原同步API不变。`async-stop`不删除数据卷或停止外部模型。
业务外发白名单仍为空，本机工作台使用LM Studio，不外发现有私人文档。

工作台 `Max tokens` 与产品问答 API 默认值、上限均为 **8192**。
这是回答 LLM 的输出预算，不是 Embedding/Reranker 的输入上下文上限；
三类预算不能互换。旧摘录/PG检索接口默认512不变，上限兼容8192。
总请求预算仍最多300秒；生成达到上限仍明确返回incomplete，不自动无限续写。

脚本加载根目录 `.env`，检查 Python 3.11+ 和依赖，启动已有 PostgreSQL 容器和
Compose Weaviate，执行 migration，启动产品 API 与同端口 Gradio 并等待就绪。
端口从8000自动避让，实际地址以输出和 `.local/runtime/service.json` 为准。
本轮地址：

- API：`http://127.0.0.1:8001`
- 前端：`http://127.0.0.1:8001/product-workbench/`
- 原7860书签：当前兼容入口会跳转至同一工作台；根启动脚本主要管理8001。
- Readiness：`GET /health/product/ready`
- 本地日志：`.local/runtime/api.log`

**前置条件：** Docker Desktop、开发 PostgreSQL、Ollama 和 LM Studio 已配置；
本地模型已下载/加载，8003 的 Jina Reranker 服务已运行。脚本不安装模型、不创建未知密码的数据库、
不启动 LM Studio GUI，不停止用户原8000服务。重复 start 复用自己的进程。

解释器选择：`RAG_PYTHON` → 本机 `enterprise-rag` Conda → `.venv` → `python3`。
本轮 `/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python`（3.11.15）。
旧 `.venv` 是3.9.6且缺依赖，不使用，也不删除。

其他机器需 Python3.11+，安装 `python -m pip install -e '.[dev,workbench,reranker]'`，
再配置 `.env.example` 中的变量。不要覆盖已有 `.env`。`Settings` 不自动读 dotenv。

## 模型与索引

**三类本地模型统一配置：** 本机 `.env` 已指定
`LOCAL_LLM_CONFIG_FILE=config/local-llm.toml`。回答模型读取顶层 `model/base_url`，
Embedding 读取 `[embedding]` 的 `model/base_url/dimension`，
Reranker 读取 `[reranker]` 的 `model/base_url`，无需改代码。修改后执行：

```bash
bash start.sh stop
bash start.sh
```

配置在启动时读取，不热更新；`model`必须与LM Studio模型ID一致，模型须在服务中
可用。文件优先于旧 LLM、Ollama Embedding 和 BGE 环境变量；
`max_concurrency/max_queue_size` 仍在顶层配置。
API Key仍只从 `.env` 的 `LOCAL_LLM_API_KEY`读取；未启用文件的旧部署保留环境变量
路径。`config/model-registry.yaml`是能力/历史验证记录，不是本机运行配置。
仅更换回答 LLM 或 Reranker 不需要重新入库；更换 Embedding 必须回填新索引。

最新公开产品样本真实上传、双路检索、Jina 重排及回答通过：
`answered / citation_validation=passed / finish_reason=stop`。
股东问题已正常结束，但仍 `invalid_citations / failed`，不能当作完整修复。
证据：`artifacts/experiments/2026-10-03_embedding-reranker/verification.json`。

**此前模型诊断：** “平安银行公司股东数量及持股情况”触发原蒸馏回答模型重复推理、
预算耗尽。相同证据的采样对照虽21.278秒结束且未重复，但引用校验失败，尚未修复。
另有两份来源文档、模型缺少标题元数据的风险。建议对照已有本地候选模型，
该诊断轮尚未切换模型，后续已按上述配置切换；历史细节见
`artifacts/experiments/2026-10-03_answer-model/README.md`，不可当作验收通过。

| 角色 | 本轮真实配置 |
|---|---|
| Embedding | Ollama `feadxus/qwen3-embedding:4b`，实测2560维，11434 |
| Reranker | `jina-reranker-v3-mlx`，HTTP `127.0.0.1:8003/v1/rerank` |
| 回答 | LM Studio `qwen3-coder-next-reap-48b-a3b-mlx`，由 `config/local-llm.toml` 读取 |
| 向量/词法 | Weaviate1.27.10，独立Dense/BM25F调用 |
| 元数据 | PostgreSQL16，migration `0004_weaviate_hybrid_generations` |

`[embedding].dimension` 必须匹配模型真实输出。新模型/维度会生成独立 Dense
profile 与物理 collection，不能混用向量。历史1024/4096维索引与实验保留，不删除。
本次已回填9份授权文档、179个Child切片；另新增公开验证样本，当前新profile有10个scope。

以后更换 Embedding 的步骤：停止受控 API、修改 TOML、执行回填，再启动 API。
回填工具仅适用于当前显式 development 免 Token 演示身份，有权读取的当前发布文档；
不用于任意租户或生产迁移。默认只输出脱敏计划，`--apply` 才写入。

```bash
bash start.sh stop
# 修改 config/local-llm.toml 的 [embedding]，模型须已在 Ollama 中可用
set -a
source .env
set +a
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.reindex_local_models
"$PYTHON" -m scripts.reindex_local_models --apply
bash start.sh
```

回填复用同一document/version，完成双路完整性检查后发布新profile指针；不覆盖旧profile。
HTTP Reranker 不在 API 进程内加载 BGE，不自动重试；可恢复故障按既有策略保留RRF并标记degraded。

## 验收与复现

最新三模型切换证据：`artifacts/experiments/2026-10-03_embedding-reranker/README.md`。
运行中API上传201、2560维持久化、Dense65/BM25F1、RRF60/Jina和完整引用回答通过。
全量离线371 passed / 12 skipped；单独真实PostgreSQL集成16 passed。

此前免 Token 证据：`artifacts/experiments/2026-10-03_no-token-text-pdf/`：
`acceptance.json`、TXT/PDF上传/回答/持久化、截图、`http-success.log`、
5类真实依赖故障注入和回归输出。完整复现命令见该目录README。
多文件最新证据：`artifacts/experiments/2026-10-03_multi-upload/`：
一次选择 TXT、Markdown、12583979 字节 PDF，生成 3 个独立 Document ID，
全部 published/active/ready，并完成 Dense/BM25F/RRF/BGE/本地 LLM 引用问答。
免Token历史轮303 passed / 12 skipped，多文件历史轮327 passed / 12 skipped；
各轮另行真实PostgreSQL16 passed。

此前默认鉴权证据：`artifacts/experiments/2026-10-03_runtime-acceptance/`。

- `metrics.json`：真实网络HTTP上传、状态、双路检索、RRF/BGE、回答及ACL测试。
- `persistence.json`：原文/manifest hash、lineage、维度、双路chunk IDs、active pointer。
- `answer.json`：公开合成文档的真实模型回答、逐字引用与 retrieval trace。
- `fallback.json`：8个ASGI HTTP故障注入用例，未替换的依赖均为真实服务。
- `browser.json` 和截图：真实浏览器工作台验收。
- 本目录 `README.md`：精确实验边界、命令及测试结果。

```bash
set -a
source .env
set +a
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.verify_no_token prepare
# 先完成实验README中的真实浏览器TXT/PDF上传和问答，再核验底层持久化/降级
"$PYTHON" -m scripts.verify_no_token verify
```

最新runner不读取Token。当前固定演示tenant为local-demo，ACL仍保留。
如需重跑历史跨tenant/撤权smoke，须在**独立鉴权服务**上设置
`P0_ANONYMOUS_ENABLED=false`；不能把依赖Token的历史smoke连到当前匿名服务。
该模式下开发Token是本项目身份凭证，不是LLM/Embedding/Reranker密钥。

干净shell中 `python -m pytest -q` 可离线回归；加载开发 `.env` 后仅运行
`python -m pytest -q tests/integration` 检查真实PostgreSQL。
不要把全部开发环境变量注入确定性单测。

## 高级工程面试材料

面向 RAG 与工程综合面试的详细问题驱动问答集见
[高级 RAG 工程面试问答集](docs/interview/advanced-rag-engineering-qa.md)。
文档严格区分已实现、已验证、历史实验和生产演进方案，避免把规划能力当成当前能力。
直接阅读 [HTML 版](docs/interview/advanced-rag-engineering-qa.html)，73 题均包含白话解析、逐步输入/处理/代码/输出、离线时序图及简版口述。Markdown 为正文源，安装开发依赖后运行 `python scripts/render_interview_doc.py` 同步生成 HTML。

## 工程入口

产品入口是 `scripts.local_p0_api:app --factory`，不是默认基础API。

| 路径 | 职责 |
|---|---|
| `/api/v1/query` | caller-supplied evidence摘录问答；不是授权检索 |
| `/api/v1/retrieval/query` | 旧PostgreSQL JSON + 应用cosine demo |
| `/api/v1/documents`、`/api/v1/rag/query` | 产品链；默认开发Bearer，本机显式匿名时固定演示身份 |

`compose.yaml` 保留基础API；`compose.p0.yaml` 提供独立产品API覆盖配置，
需显式环境变量。本次是本机Conda API + 依赖容器，未重新验证Docker产品镜像。
`不用的说明和脚本/` 保留历史排障资料，仅恢复仍被引用的 `compose.p0.yaml`；
`scripts/gradio_enhanced.py` 已改为本机7860跳转兼容入口，不再调用旧摘录问答。

## 交接与边界

新会话依次读 `PROJECT_CONTEXT.md`、`CLAUDE.md`、最新三模型切换实验README、
`.planning/2026-10-03-runtime-acceptance/` 和 `.planning/handoff/HANDOFF.md`。
专题文档见 `docs/README.md`。

实际解析器为自研UTF-8 TXT/Markdown/HTML/PyPDF2；Parent800/Child320 **字符**，无overlap。
两路索引共用generation/active pointer，不是独立发布。
Sparse-only降级使用Child正文，部分locator可能为空。
检索和最终ACL继承同一deadline；HTTP取消只证明本地等待/传输取消，不保证服务端立即停止推理。

尚未接入：Keycloak/OIDC、Redis、Celery/RabbitMQ、Git同步、OCR、Agentic Rewrite、
生产队列、容量/SLO、HA、备份恢复及在线Provider fallback。
小样本通过不是完整RAG质量结论。开发HMAC token不是企业SSO。
匿名模式默认关闭，仅development与loopback可用；共享固定身份不是企业多用户鉴权。
配置改动后stop/start，重复start只复用原进程、不热加载.env。
`.env`、token、对象存储和本地日志不可提交；参阅 `SECURITY.md`。

目标仍是P0完整、P1关键实验证据、P2可演进。License：MIT。
