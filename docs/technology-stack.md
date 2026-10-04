# 技术栈与运行时方案

状态标记：`已确认`、`待验证`、`候选`、`非目标`。

**实施快照（2026-10-03）：** Python3.11.15/FastAPI/SQLAlchemy/asyncpg、
Gradio6.29.0、PostgreSQL16、Weaviate1.27.10、本地文件对象存储、
Ollama bge-m3/1024、BGE v2-m3 CPU、LM Studio本地回答已实际连通。
Migration为Python contract，不是Alembic。
最新追加：UTF-8 TXT和308279字节文本型PDF免Token UI真实通过；
匿名默认关闭，本机显式开启后固定演示身份，不涉及模型Token，ACL仍保留。
产品multipart、Gradio临时文件和queue/join批量元数据不设项目层面的数量/大小上限，
普通API128KiB；当前7860只跳转8001。实际仍受机器、解析器和下游模型能力限制。
下表“已确认”包含架构选择，不表示Celery/Redis/OIDC/Minikube/
LinearAsyncExecutor/LangGraph已经实现。当前parser/splitter和在线编排为自研，
实际Parent800/Child320字符、overlap0；下述200与候选数量是历史实验起点。

## 应用与编排

| 层次 | 方案 | 状态 | 说明 |
|---|---|---|---|
| API | Python + FastAPI | 已确认 | 在线请求、管理 API、健康检查 |
| 调试端 | Gradio | 已确认 | 内部调试和管理，不作为权限边界 |
| 在线协议 | REST + SSE | 已确认 | SSE 事件必须有 run_id 和生命周期 |
| 任务 | Celery + RabbitMQ | 已确认 | 摄取和后台任务；必须幂等、分类重试 |
| 线性执行 | 原生 asyncio LinearAsyncExecutor | 已确认 | 清晰的 deadline、取消和中间结果 |
| 复杂执行 | LangGraph State/Node/Edge/Checkpointer | 已确认 | 仅有界 Agentic 工作流，不使用开放式 Agent |

## 数据与基础设施

| 组件 | 方案 | 状态 | 职责 |
|---|---|---|---|
| 业务数据库 | PostgreSQL | 已确认 | 元数据、租户、ACL、版本、任务、审计、Checkpoint |
| 对象存储 | 受维护的 S3 兼容实现 | 待验证 | 原文和解析产物；MinIO 不再作为无条件默认 |
| 向量/词法检索 | Weaviate | 已确认 | Dense 与 BM25F 分别调用；版本实施时锁定 server/client/chart/digest |
| 缓存/锁 | Redis | 已确认 | 缓存、限流、锁、短期状态；不承担唯一长期事实 |
| 身份 | Keycloak + OIDC | 已确认 | 身份、登录、粗粒度角色；业务 ACL 由应用校验 |
| 开发部署 | Docker Compose | 已确认 | 本地开发和集成测试 |
| K8s 验证 | Minikube | 已确认 | 镜像、探针、持久化、基础故障演练；不等于独立故障域 |

## 模型运行时

| 角色 | 当前候选 | 推荐策略 |
|---|---|---|
| Embedding | Ollama 本地模型 | 已部署，首期直接使用；运行时通过 Provider Adapter 隔离 |
| Reranker | 工程内 `BGEReranker` + `SyncRerankerAdapter` | 已确认 | Python 进程内调用；同步模型通过线程适配为异步 Provider，不再启动 Ollama Reranker 服务 |
| 最终回答 LLM | LM Studio + 本地 Qwen；在线 Grok/GPT | 本地默认开发和压测，在线小规模质量对照 |
| Router/Grade/Rewrite | 小型本地模型；`omlx` 候选 | 先做契约与并发基准；不能因 MLX 格式自动引入 `omlx` |
| 网关 | LiteLLM 或统一 Provider Adapter | 对外隐藏供应商差异；保留能力、错误和数据边界检查 |
| 替身 | Fake LLM | 单测、状态机、故障和高并发系统上限测试 |

## 检索与文本处理

```text
Parser → Cleaner → Chunker → Enricher
       → Embedding / BM25F index → 发布版本
```

- Parent 约 800、Child 约 200 作为初始实验点；`overlap=0` 作为基线，不视为定论。
- Child 召回，Parent 上下文；Parent 回溯后重新检查 ACL 和文档版本。
- Dense 与 BM25F 分别查询，RRF 等权 `k=60` 作为起点；对照 `k=20/100`。
- 初始候选：两路各 50 → RRF 约 40 Child → Reranker → Parent 去重 → 最多 5 Parent 并受 token 预算约束。
- BM25F 初始权重候选：`title=2, section_title=2, api_symbols=3, body=1`，必须结合中文和技术标识符分词实测。

## 运行约束

- 本地模型并发必须经过基准测试；应用侧有界队列、在线/后台隔离和取消语义。
- 同一模型服务可接收并发请求不等于真正并行生成；记录排队、首 token、生成和完成延迟。
- Embedding/Reranker/LLM 结果均带模型和配置版本；更换 Embedding 建新索引空间，不混用向量。
