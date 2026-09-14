<!-- 项目入口：新会话优先读取本文件，再按任务读取 docs/ 下的专题文档。 -->
# EnterpriseKnowledgeRAG 项目上下文

## 1. 项目目标

从零开发一个面向研发技术文档的企业级 RAG 知识库，用于高级大模型开发岗面试。目标不是冒充真实生产平台，而是完成：

- **P0：** 可运行的端到端闭环；
- **P1：** 检索、权限、可靠性、性能等关键能力有可复现实验证据；
- **P2：** 有清晰、带触发条件和代价说明的生产演进方案。

最终交付目标：**P0 完整 + P1 关键证据 + P2 可演进**。

## 2. 当前状态（2026-09-14）

- 业务 RAG 闭环仍未完成；Phase 3 的 PostgreSQL 基础切片已实现数据模型、异步 session、首个 migration contract、状态机和版本发布事务边界。
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
- 显式移除 `POSTGRES_DSN` 的离线全量测试为 `36 passed, 7 skipped`；加载项目根目录 `.env` 中有效开发 DSN 后全量测试为 `43 passed`，其中 `tests/integration/test_postgres.py` 的 7 个真实 PostgreSQL 场景通过。真实应用 `/health/ready` 返回 `200` 并报告 migration `0001_initial`；Ruff、compileall 和 diff 检查均通过。
- 环境预检：`docs/preflight-checklist.md`；用户已验证 Docker Desktop 正常（`linux/aarch64`、16 CPU、15.6 GiB），Minikube 未安装，`kubectl` 已安装。Codex 沙箱因 socket 权限可能无法复跑 Docker API。
- 本轮真实验证使用固定镜像 `postgres:16`，容器内 PostgreSQL 版本为 `16.15`，Docker Server 为 `linux/arm64`；测试结果仅证明当前 schema、事务和发布锁场景，不代表生产容量或高并发性能。
- PostgreSQL 连接池已按单机/CI 保守参数设计：`pool_size=3`、`max_overflow=2`、`pool_timeout=10s`、`pool_recycle=1800s`、`pool_pre_ping=True`；事务超时使用 `SET LOCAL`，默认 `statement_timeout=30s`、`lock_timeout=5s`。
- migration 由 Compose/部署 one-shot init container 执行；API `/health/ready` 只检查连接和目标 schema/migration 版本，不自动迁移；CI 已增加 PostgreSQL migration/integration job 配置。
- PostgreSQL 集成测试继续使用同一开发容器，但每个测试创建独立临时数据库并在结束后删除，不再清理固定库的 `public` schema。
- Compose 已完成动态验证：one-shot migration 成功时 API 按 `service_completed_successfully` 启动并通过 `/health/ready`，不可达数据库时 migration 非零退出且 API 不启动；验证使用临时 Docker 配置目录绕过用户级 credential helper 拉取问题，不修改用户级配置。
- Phase 3 连接可靠性与部署集成切片已完成；下一步进入 Phase 4/5 的真实服务契约、摄取基础、评测/压测和交付文档，不提前扩展检索或 Agentic 范围。

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
