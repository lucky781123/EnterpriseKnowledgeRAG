# 本地最小 P0 产品闭环实施计划

**Goal:** 实现用户已确认的方案 C：`上传 -> 同步摄取 -> 索引发布 -> 开发身份 ACL -> 混合检索 -> 问答引用`，并用自动化测试和一次真实依赖 smoke 证明本地最小产品闭环可运行。

**Architecture:** FastAPI 只接收可信开发 token 和产品参数；`IngestionService` 生成不可变 manifest；Weaviate 保存同一 generation 下的 Dense/BM25F 派生对象；PostgreSQL 保存 tenant、文档、版本、ACL、artifact、generation 和 active pointer 事实；查询先解析授权 scope，再执行 Dense+BM25F+RRF+BGE Reranker，最后回溯 Parent、重复授权并复用 `ExcerptAnswerService` 生成带引用回答。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic、SQLAlchemy asyncio、PostgreSQL、Weaviate 1.27.10、Ollama Embedding、进程内 BGE Reranker、LM Studio、pytest/pytest-asyncio、Ruff。

**Spec:** `docs/superpowers/specs/2026-09-17-local-p0-product-closure-design.md`

## 全局约束

- 所有实现先写失败测试，再写最小实现；每个方法必须有详细中文注释。
- 产品请求不能接受 `tenant_id`、`actor_id`、`version_id`、evidence、Provider endpoint、API key 或模型服务地址。
- PostgreSQL 是授权和发布事实源；Weaviate 不作为权限、当前版本或 active generation 的唯一事实源。
- Dense、Sparse、RRF、Reranker、Parent、引用必须使用同一或更严格的授权 scope。
- 未发布、未完整或未授权 generation 不可查询；无授权证据时不得调用 Reranker 或 LLM。
- 默认 Sparse profile 固定为 `word + identity`；不因小样本实验把 `cjk_bigram` 切为默认。
- Keycloak/OIDC、Celery/RabbitMQ、Redis、Git 同步、Minikube 和生产容量不在本计划内。
- 不读取、打印、提交或写入报告中的 `.env`、DSN、token secret、密码或 API key。

## 成功标准

1. **身份与 ACL** -> 验证：篡改/过期/跨租户/IDOR/撤权测试全部 fail closed。
2. **上传与发布** -> 验证：multipart 上传同步完成 RAW/PARSED/EMBEDDING/SPARSE，双路 identity 一致后才发布。
3. **混合检索** -> 验证：产品 API 实际执行 Dense+BM25F+RRF+BGE Reranker，并保留通道 rank provenance。
4. **问答引用** -> 验证：Parent 回溯和引用返回前重新授权；无授权 evidence 时 LLM 调用次数为零。
5. **本地运行** -> 验证：Compose/readiness、工作台和真实 PostgreSQL/Weaviate/Ollama/BGE/LM Studio smoke 可复现。

## Task 1: 开发身份与可信请求上下文

**Files:**
- Create: `src/enterprise_rag/security/context.py`
- Create: `src/enterprise_rag/security/dev_token.py`
- Create: `src/enterprise_rag/security/__init__.py`
- Modify: `src/enterprise_rag/config/settings.py`
- Modify: `.env.example`
- Test: `tests/unit/test_dev_auth.py`
- Test: `tests/unit/test_settings.py`

- [ ] RED：锁定签名、篡改、过期、未来 `iat`、非法 claim、错误 secret、角色去重和不可变 `RequestContext`。
- [ ] GREEN：用标准库实现 URL-safe Base64、HMAC-SHA256、`compare_digest()` 和严格 claim 校验。
- [ ] GREEN：增加 `DEV_AUTH_ENABLED`、`DEV_AUTH_SECRET`、`DEV_AUTH_TOKEN_TTL_SECONDS`；非 development 环境启用时启动失败。
- [ ] VERIFY：运行 `tests/unit/test_dev_auth.py tests/unit/test_settings.py`、Ruff 和 compileall。

## Task 2: FastAPI 身份依赖与 ACL 授权服务

**Files:**
- Create: `src/enterprise_rag/security/authorization.py`
- Create: `src/enterprise_rag/api/dependencies.py`
- Create: `src/enterprise_rag/api/product_schemas.py`
- Modify: `src/enterprise_rag/api/app.py`
- Test: `tests/unit/test_authorization.py`
- Test: `tests/api/test_product_auth_api.py`

- [ ] RED：锁定缺失/非法 token、角色矩阵、同 tenant allow、跨 tenant deny、资源不存在和 IDOR 统一 404。
- [ ] GREEN：从 Bearer token 建立 `RequestContext`，不读取 tenant header 或请求体 tenant。
- [ ] GREEN：实现上传者自动 READ ACL、admin grant/revoke、授权文档和 active generation scope 查询。
- [ ] VERIFY：撤权后同一进程下一次查询立即拒绝，不依赖 Weaviate 清理。

## Task 3: 双路 generation 完整性与发布事务

**Files:**
- Modify: `src/enterprise_rag/retrieval/contracts.py`
- Modify: `src/enterprise_rag/retrieval/sparse.py`
- Modify: `src/enterprise_rag/retrieval/weaviate_sparse.py`
- Modify: `src/enterprise_rag/retrieval/generation.py`
- Modify: `src/enterprise_rag/db/publish.py`
- Test: `tests/unit/test_product_generation.py`
- Test: `tests/integration/test_product_publish_postgres.py`

- [ ] RED：锁定 Dense/Sparse count、chunk identity、tenant/version/generation/profile 任一不一致时拒绝发布。
- [ ] GREEN：为 tenant 派生稳定 Sparse collection 名称；复用同一 generation ID 和 child identity。
- [ ] GREEN：在发布事务前将 EMBEDDING/SPARSE artifact 标记 READY；事务内切换 document version 与 generation pointer。
- [ ] VERIFY：发布失败时旧 version/generation 保持 active；未发布新对象不可查询。

## Task 4: 同步产品上传编排与 multipart API

**Files:**
- Create: `src/enterprise_rag/ingestion/product_service.py`
- Modify: `src/enterprise_rag/api/product_schemas.py`
- Modify: `src/enterprise_rag/api/app.py`
- Test: `tests/unit/test_product_ingestion.py`
- Test: `tests/api/test_document_upload_api.py`

- [ ] RED：锁定 editor/admin 上传、reader 拒绝、MIME/大小校验、幂等复用、内容冲突和失败不发布。
- [ ] GREEN：同步复用 `IngestionService`、manifest、Dense indexer、Sparse projector/transport、完整性校验和发布事务。
- [ ] GREEN：新增 `POST /api/v1/documents`，返回 document/version/job/generation/artifact/publish 状态。
- [ ] VERIFY：请求取消或单路失败只留下不可查询 generation；稳定 idempotency key 可安全重试。

## Task 5: 授权 scope 与混合检索服务

**Files:**
- Create: `src/enterprise_rag/retrieval/scope.py`
- Create: `src/enterprise_rag/retrieval/hybrid.py`
- Modify: `src/enterprise_rag/retrieval/weaviate_sparse.py`
- Modify: `src/enterprise_rag/reranking/adapters.py`
- Test: `tests/unit/test_hybrid_retrieval.py`
- Test: `tests/integration/test_product_acl_retrieval.py`

- [ ] RED：锁定授权 scope、Dense/Sparse 双路归因、RRF 稳定排序、候选去重、Reranker 映射和 Parent 去重。
- [ ] GREEN：每个问题只生成一次 embedding；只查询授权 active generation；RRF 默认 `k=60`。
- [ ] GREEN：BGE Reranker 必须成功；失败返回 `reranker_unavailable`，不静默降级。
- [ ] VERIFY：两个 tenant 相同文本和 query 零串租户；无授权 scope 时不访问 Weaviate、Reranker 或 LLM。

## Task 6: 产品问答、Parent 回溯和引用复核

**Files:**
- Modify: `src/enterprise_rag/api/product_schemas.py`
- Modify: `src/enterprise_rag/api/app.py`
- Modify: `src/enterprise_rag/excerpts/service.py`
- Test: `tests/api/test_product_rag_query_api.py`
- Test: `tests/unit/test_product_citations.py`

- [ ] RED：锁定请求字段白名单、当前版本复核、Parent ACL 复核、引用 identity/quote/ACL 复核和无 evidence 不调用 LLM。
- [ ] GREEN：新增 `POST /api/v1/rag/query`，转换授权 Parent 为现有 F2-G evidence 并复用调度器和引用校验。
- [ ] GREEN：响应返回回答、引用和不含正文/内部 collection/key 的安全 retrieval trace。
- [ ] VERIFY：撤权后旧 citation 详情也返回 404；文档内 prompt injection 文本只作为 evidence data。

## Task 7: 状态、ACL 管理接口与产品 readiness

**Files:**
- Modify: `src/enterprise_rag/api/product_schemas.py`
- Modify: `src/enterprise_rag/api/app.py`
- Modify: `src/enterprise_rag/config/settings.py`
- Test: `tests/api/test_product_management_api.py`
- Test: `tests/api/test_app.py`

- [ ] RED：锁定文档/版本/任务/generation 状态查询、admin ACL grant/revoke 和 readiness 缺依赖行为。
- [ ] GREEN：产品 readiness 显式检查 PostgreSQL、Weaviate、Embedding、Reranker、LM Studio、对象存储和开发身份配置。
- [ ] VERIFY：基础 liveness 不调用外部依赖；产品依赖不完整时返回稳定 not ready。

## Task 8: 工作台、Compose、真实 smoke 与文档收口

**Files:**
- Modify: `src/enterprise_rag/workbench/app.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Create: `scripts/local_p0_smoke.py`
- Create: `tests/test_local_p0_smoke.py`
- Modify: `README.md`
- Modify: `PROJECT_CONTEXT.md`
- Modify: `docs/roadmap.md`
- Modify: `docs/acceptance-matrix.md`

- [ ] RED/GREEN：工作台只调用 FastAPI，支持 token、上传、状态和问答，不直连数据库或模型服务。
- [ ] RED/GREEN：smoke 报告只保存脱敏阶段、数量、状态、延迟和越权零命中，不保存 token、正文或凭据。
- [ ] REAL：在专用开发 PostgreSQL、Weaviate 1.27.10、Ollama、BGE 和 LM Studio 上完成上传到引用、跨租户拒绝、撤权和恢复。
- [ ] VERIFY：全量 pytest、Ruff、compileall、Compose config、Skill validator、`git diff --check` 和敏感信息审计通过。

## 用户参与点

- 真实 smoke 前确认 Docker Desktop、专用开发 PostgreSQL、Ollama 和 LM Studio 正常。
- 确认本机已有 BGE Reranker 模型；若缺失，需要用户允许下载模型缓存。
- 在本机安全生成至少 32 字节 `DEV_AUTH_SECRET` 并写入未跟踪的 `.env`；不得通过聊天或提交暴露。
- 代码、Fake/contract 测试和离线回归不依赖以上操作，可先完成。
