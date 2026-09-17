<!-- 项目入口：新会话优先读取本文件，再按任务读取 docs/ 下的专题文档。 -->
# EnterpriseKnowledgeRAG 项目上下文

## 1. 项目目标

从零开发一个面向研发技术文档的企业级 RAG 知识库，用于高级大模型开发岗面试。目标不是冒充真实生产平台，而是完成：

- **P0：** 可运行的端到端闭环；
- **P1：** 检索、权限、可靠性、性能等关键能力有可复现实验证据；
- **P2：** 有清晰、带触发条件和代价说明的生产演进方案。

最终交付目标：**P0 完整 + P1 关键证据 + P2 可演进**。

## 2. 当前状态（2026-09-17）

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
- 历史 PostgreSQL/F2-G 基线曾记录离线 `126 passed, 11 skipped` 和有效开发 DSN `137 passed`；完成 BM25F 中文对照实验后，最新离线全量为 `200 passed, 11 skipped`。本轮未注入 `POSTGRES_DSN`，因此 PostgreSQL 集成仍明确跳过；Ruff、compileall、Compose config、Skill suite validator 和 diff 检查均通过。
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
- F2-G 专项和真实摄取基础的历史证据保持不变；当前最新离线全量为 `200 passed, 11 skipped`。真实 LM Studio/F2-G 观测仍按原实验产物解释，不外推为生产质量或容量。
- F2-G 当前边界仍明确：尚未接入完整 Dense+BM25F+RRF 默认检索链、ACL/OIDC 闭环、在线 Provider 网络适配器或生产容量验证；真实本地 benchmark 不代表语义质量、成本、SLO、GPU 行为或生产吞吐。
- 真实摄取基础已实现：支持 Markdown、HTML 和受控文本型 PDF 的校验与统一解析，生成带标题路径/locator 的 `ParsedBlock` 以及确定性 Parent/Child chunk；原文和 parsed manifest 写入本地对象存储，PostgreSQL 保存 job/version/artifact 状态。
- 摄取可靠性已验证：同租户幂等键复用、内容冲突、FAILED 显式 replay、同内容 version 复用和共享 version 并发成功/失败均有测试；真实 PostgreSQL 并发失败证据证明 parser 只执行一次。摄取成功不会自动设置 `documents.current_version_id`。
- 真实摄取基础边界：不包含上传/Git 同步 API、OCR、Embedding、BM25F/Weaviate、ACL/OIDC、队列 Worker、自动发布或完整检索问答链；parsed manifest 不是检索索引。
- 方案 B 最小检索闭环已通过真实 smoke：固定 Markdown 经 Ollama Embedding 写入 PostgreSQL JSON，应用层执行 cosine，按 tenant/version 过滤并回溯 Parent，再通过 FastAPI 调用 LM Studio；证据见 `artifacts/experiments/2026-09-16_retrieval-smoke/`。
- 方案 B 当前边界：默认向量检索仍是 PostgreSQL JSON + 应用层 cosine 的演示路径；Weaviate Dense/BM25F 已有独立真实 smoke 和固定小数据集质量实验，但尚未接入默认 Dense+BM25F+RRF 链、ACL/OIDC 请求闭环或生产容量验证。
- Weaviate Dense 迁移切片已实现：每个 tenant 独立 collection、generation scope、PostgreSQL active/previous pointer、批量 checkpoint、完整性校验、回填、retire、cleanup 和显式 repository 注入；默认 API 读取路径仍保持 PostgreSQL。
- Weaviate Dense 已在 Compose `1.27.10` 取得真实 transport smoke：collection、批量写入、完整性、nearVector 查询和删除均通过；GraphQL `limit` variable 与 minimal 删除计数的 1.27 兼容差异已修复。PostgreSQL active pointer 发布/回滚未纳入本次 transport-only smoke，默认读取路径仍未切换。
- BM25F Spike 已实现确定性字段投影和最小真实 transport：`title`、`section_title`、`api_symbols`、`body` 带权查询在真实 Weaviate 命中，错误 tenant/version 均返回 0；尚未新增 Sparse Generation 数据库状态机或接入默认 API。证据见 `artifacts/experiments/2026-09-17_weaviate-real-smoke/`。
- BM25F/RRF 固定小数据集质量实验已完成：真实 Ollama `qwen3-embedding:8b` + Weaviate `1.27.10` 上，10 个合成 chunk、8 条查询的 Dense @3 Recall/MRR/nDCG 为 `0.9375/1.0/0.9516`，BM25F 为 `0.625/0.625/0.625`，RRF `k=20/60/100` 均为 `1.0/1.0/1.0`；Dense 独有命中 3 条语义改写，BM25F 在双相关查询中补足一个 Dense rank 4 证据。证据见 `artifacts/experiments/2026-09-17_bm25f-rrf-quality/`。
- BM25F 中文 tokenizer/预处理对照已完成：保持 Dense、字段权重、candidate K 和 RRF 不变，`word + identity` BM25F @3=`0.625/0.625/0.625`，`trigram + identity`=`0.75/0.75/0.75`，`word + cjk_bigram`=`1.0/1.0/1.0`；对应产物见 `artifacts/experiments/2026-09-17_bm25f-chinese-tokenization/`。
- 中文对照只证明当前 10 个合成 chunk、8 条查询上的字符重叠收益：`trigram` 仍漏掉 2 条语义改写并引入低分噪声，`cjk_bigram` 满分可能过拟合。生产默认 profile、人工确认 development/frozen 数据集、索引体积、容量和 SLO 仍待实验。

## 3. 已确认技术边界

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
