# Weaviate Dense 迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 `POST /api/v1/retrieval/query` 契约且不提前切换默认读取路径的前提下，实现方案 6A 的 tenant 独立 Weaviate collection、generation 构建/回填、完整性校验、发布指针、回滚和可重建 Dense 查询适配。

**Architecture:** PostgreSQL 新增 tenant collection registry、Dense generation 和 active pointer 事实模型；Weaviate 通过独立 transport/repository 作为可重建派生索引。每个 tenant 使用一个 collection，每个 `(tenant_id, version_id, index_profile)` 使用不可变 generation；只有 PostgreSQL 发布指针指向的 generation 可读。现有 PostgreSQL JSON + 应用层 cosine 继续作为默认后端，Weaviate 只通过显式依赖注入和 smoke/contract tests 接入。

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, PostgreSQL/SQLite test contract, HTTPX async transport, Ollama embedding provider, Weaviate REST contract, pytest/pytest-asyncio, Ruff。

**Spec:** `docs/superpowers/specs/2026-09-16-weaviate-dense-migration-design.md`

## Global Constraints

- PostgreSQL 是 tenant、document、version、ACL、artifact 状态和发布事实的唯一事实源。
- Weaviate 只保存可重建的 Dense 派生数据，不作为权限或版本唯一事实源。
- 每个 tenant 使用独立 Weaviate collection；generation 作用域固定为 `(tenant_id, version_id, index_profile)`。
- PostgreSQL 与 Weaviate 不宣称跨系统原子事务；采用 PostgreSQL 事实先行、Weaviate 异步构建和补偿。
- `BUILDING`、未校验的 `READY`、未发布 generation 不可查询；fallback 只能指向已验证的旧 active generation。
- 保持现有 `POST /api/v1/retrieval/query` 请求/响应契约；本计划不切换默认线上读取路径。
- 不实现 BM25F、RRF、reranker、ACL/OIDC 请求闭环、Celery/RabbitMQ、Redis、OCR、上传/Git 同步或生产容量结论。
- 所有新增方法必须有详细中文注释；所有状态转换、调用顺序、错误边界和测试 fixture 必须有中文说明。
- 不读取、打印或持久化 `.env`、DSN、密码或 API key；真实 smoke 只输出脱敏状态和指标。

---

## 文件边界

- `src/enterprise_rag/domain/entities.py`：新增 Dense generation 状态枚举。
- `src/enterprise_rag/db/models.py`：新增 tenant collection registry、generation、active pointer 模型。
- `src/enterprise_rag/db/state.py`：新增 generation 状态机和 attempt fencing 校验。
- `src/enterprise_rag/db/migrations.py`：新增 `0003_weaviate_dense_generations` migration contract。
- `src/enterprise_rag/retrieval/contracts.py`：新增 `IndexProfile`、generation scope、Weaviate object 和构建结果契约。
- `src/enterprise_rag/retrieval/weaviate.py`：新增 Weaviate transport protocol、HTTPX adapter 和固定错误归一化。
- `src/enterprise_rag/retrieval/generation.py`：新增 collection registry、generation 创建、checkpoint、完整性检查和发布/回滚服务。
- `src/enterprise_rag/retrieval/indexer.py`：保留现有 PostgreSQL `DenseIndexer`，新增按 generation 分批写入的 `WeaviateDenseIndexer`。
- `src/enterprise_rag/retrieval/repository.py`：新增 Weaviate repository；保留 `PostgresVectorRepository` 作为默认实现。
- `src/enterprise_rag/db/publish.py`：新增 Dense generation 发布/回滚事务入口，不放宽现有 document version 发布前置条件。
- `src/enterprise_rag/config/settings.py`：新增 Weaviate endpoint、API key 和显式启用配置，但默认关闭。
- `src/enterprise_rag/api/app.py`：只增加显式注入的 Weaviate repository/generation service 状态，不改变默认读取路径。
- `tests/unit/test_weaviate_generation.py`：状态机、命名、幂等键、完整性检查和 fencing 测试。
- `tests/unit/test_weaviate_transport.py`：HTTPX contract、批量 upsert、重复写入和错误归一化测试。
- `tests/unit/test_weaviate_indexer.py`：manifest scope、checkpoint、批次重试和 parent metadata 测试。
- `tests/integration/test_weaviate_postgres.py`：真实 PostgreSQL registry/generation/publish/rollback 集成测试；无 DSN 时跳过。
- `tests/api/test_retrieval_weaviate_injection.py`：查询契约不变和未发布 generation 不可读测试。
- `scripts/weaviate_smoke.py`：可选真实 Weaviate 小数据 smoke；不读取或写出敏感凭据。
- `artifacts/experiments/<date>_weaviate-dense-smoke/`：只保存脱敏 metrics、README 和结论。
- `docs/architecture.md`、`docs/risk-register.md`、`docs/roadmap.md`、`docs/acceptance-matrix.md`、`PROJECT_CONTEXT.md`：实现和验证完成后同步边界，不提前写成已验证事实。

## 执行顺序

### Task 1: Dense generation 领域契约和 PostgreSQL 事实模型

**Files:**
- Modify: `src/enterprise_rag/domain/entities.py`
- Modify: `src/enterprise_rag/db/models.py`
- Modify: `src/enterprise_rag/db/state.py`
- Create: `src/enterprise_rag/retrieval/contracts.py`
- Test: `tests/unit/test_weaviate_generation.py`

**Interfaces:**
- Produces `DenseGenerationStatus`, `IndexProfile`, `GenerationScope`, `DenseGenerationRecord`、`CollectionRegistryRecord` 和 `ActiveGenerationPointer`。
- `IndexProfile.key` 必须由 `model_name`, `dimension`, `chunking_version`, `schema_version` 稳定派生。
- `GenerationScope` 必须包含 `tenant_id`, `document_id`, `version_id`, `index_profile_key`。

- [x] **Step 1: Write the failing test**

```python
def test_index_profile_key_and_collection_name_are_stable() -> None:
    profile = IndexProfile(
        model_name="demo-embedding",
        dimension=2,
        chunking_version="parent-child-v1",
        schema_version="dense-v1",
    )
    scope = GenerationScope("tenant/a", "doc-1", "version-1", profile.key)

    assert profile.key == "demo-embedding:2:parent-child-v1:dense-v1"
    assert collection_name_for_tenant(scope.tenant_id) == collection_name_for_tenant("tenant/a")
    assert collection_name_for_tenant(scope.tenant_id).startswith("DenseTenant_")
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_weaviate_generation.py::test_index_profile_key_and_collection_name_are_stable -q`

Expected: FAIL because the new contracts and collection-name helper do not exist。

- [x] **Step 3: Write minimal implementation**

实现 `IndexProfile`、`GenerationScope` 和 `collection_name_for_tenant()`；在 `domain/entities.py` 增加 `BUILDING/READY/ACTIVE/RETIRED/FAILED` 枚举；在 `db/models.py` 增加：

- `TenantDenseCollectionModel(tenant_id, collection_name, schema_version, status, created_at, updated_at)`；
- `DenseGenerationModel(generation_id, tenant_id, document_id, version_id, index_profile_key, collection_name, manifest_hash, model_name, dimension, chunking_version, schema_version, expected_item_count, actual_item_count, checkpoint, build_attempt, worker_lease, status, created_at, published_at, retired_at, failure_reason)`；
- `DenseGenerationPointerModel(tenant_id, document_id, version_id, index_profile_key, active_generation_id, previous_generation_id, updated_at)`。

为每个 composite scope、collection name、generation id 和 pointer 增加必要的唯一约束/外键。所有新增字段的模型方法和状态转换方法写中文注释。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_weaviate_generation.py -q`

Expected: PASS for profile key、collection naming、scope equality、状态枚举和值对象序列化。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/domain/entities.py src/enterprise_rag/db/models.py src/enterprise_rag/db/state.py src/enterprise_rag/retrieval/contracts.py tests/unit/test_weaviate_generation.py
git commit -m "增加 Dense generation 事实契约"
```

### Task 2: Migration contract 与 generation 状态事务

**Files:**
- Modify: `src/enterprise_rag/db/migrations.py`
- Modify: `src/enterprise_rag/db/state.py`
- Modify: `src/enterprise_rag/db/publish.py`
- Test: `tests/integration/test_weaviate_postgres.py`
- Test: `tests/unit/test_weaviate_generation.py`

**Interfaces:**
- Produces `WEAVIATE_DENSE_VERSION = "0003_weaviate_dense_generations"`。
- Produces `create_generation(session, scope, profile, manifest_hash, expected_item_count, build_attempt, worker_lease)`、`mark_generation_ready(session, generation_id, actual_item_count)`、`publish_dense_generation(session, generation_id)` 和 `rollback_dense_generation(session, pointer_scope, target_generation_id)` 的事务语义。
- `publish_dense_generation()` 只更新 Dense pointer；不得放宽现有 `publish_version()` 对所有 `ArtifactKind` 的要求。

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_generation_tables_and_pointer_are_created_after_0003(database):
    await database.migrate()
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
    assert {"tenant_dense_collections", "dense_generations", "dense_generation_pointers"} <= tables
```

测试文件顶部显式导入 `inspect`，并使用现有 `Database` fixture；不得新增未定义的数据库 helper。

另写失败测试锁定：发布 `BUILDING`/未完整 `READY` generation 抛出 `DenseGenerationPublishError`；发布事务失败时 `active_generation_id` 保持旧值；回滚只改变 pointer。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_weaviate_generation.py tests/integration/test_weaviate_postgres.py -q`

Expected: FAIL because `0003` 和 generation transaction service 尚不存在。

- [x] **Step 3: Write minimal implementation**

将 `0003` 接到现有 `SUPPORTED_VERSIONS`、`LATEST_VERSION` 和 `EXPECTED_TABLES`；保证 `0001 -> 0002 -> 0003` 顺序升级，重复 migration 幂等。实现 generation state transition：

```python
def transition_generation_status(
    current: DenseGenerationStatus,
    target: DenseGenerationStatus,
) -> DenseGenerationStatus:
    """验证 Dense generation 生命周期，不允许未发布状态直接回滚或恢复。"""
```

在 `db/publish.py` 增加显式 Dense 发布/回滚函数：锁定 pointer 和目标 generation，检查 `READY`、item count、manifest hash、profile、version 状态以及 attempt fencing，通过同一 PostgreSQL transaction 更新 active/previous 指针。失败时不提交 pointer。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_weaviate_generation.py tests/integration/test_weaviate_postgres.py -q`

Expected: SQLite contract 全部通过；设置有效专用 `POSTGRES_DSN` 时 PostgreSQL migration、并发 pointer 锁和 rollback 集成通过；没有 DSN 的测试明确 skip。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/db/migrations.py src/enterprise_rag/db/state.py src/enterprise_rag/db/publish.py tests/unit/test_weaviate_generation.py tests/integration/test_weaviate_postgres.py
git commit -m "增加 Weaviate generation migration 与发布事务"
```

### Task 3: Weaviate transport contract 与固定错误边界

**Files:**
- Create: `src/enterprise_rag/retrieval/weaviate.py`
- Modify: `src/enterprise_rag/retrieval/contracts.py`
- Modify: `src/enterprise_rag/config/settings.py`
- Test: `tests/unit/test_weaviate_transport.py`

**Interfaces:**
- Produces `WeaviateObject`、`WeaviateBatchResult`、`WeaviateTransport` protocol。
- Required methods:

```python
async def ensure_collection(self, collection_name: str, schema_version: str) -> None: ...
async def upsert_batch(self, collection_name: str, objects: Sequence[WeaviateObject]) -> WeaviateBatchResult: ...
async def count_generation(self, collection_name: str, *, generation_id: str, version_id: str) -> int: ...
async def list_chunk_ids(self, collection_name: str, *, generation_id: str, version_id: str) -> set[str]: ...
async def search(self, collection_name: str, *, generation_id: str, version_id: str, query: Sequence[float], top_k: int) -> list[RetrievalResult]: ...
async def delete_generation(self, collection_name: str, *, generation_id: str, version_id: str) -> int: ...
```

- [x] **Step 1: Write the failing test**

使用 `httpx.MockTransport` 写测试：collection 创建请求包含固定 schema；重复 upsert 返回同一 `chunk_id` 不增加逻辑数量；批次部分失败映射为可重试错误；401/429/5xx 不回显 URL、header 或 response body。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_weaviate_transport.py -q`

Expected: FAIL because transport protocol、HTTPX adapter 和错误类型尚不存在。

- [x] **Step 3: Write minimal implementation**

使用现有 `httpx.AsyncClient` 实现 adapter；所有请求由内部固定 base URL、collection name 和 API key 构造，调用方不能提交任意 endpoint。为每个方法增加中文 docstring，统一抛出不含凭据的 `WeaviateTransportError(kind, retryable, safe_message)`。查询结果必须把 `tenant_id`、`document_id`、`version_id`、`generation_id`、`chunk_id`、`parent_id`、text、locator 和 score 映射回 `RetrievalResult` 所需字段。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_weaviate_transport.py -q`

Expected: PASS，且 `git grep`/测试断言中不出现真实 DSN、API key 或完整异常正文。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/retrieval/weaviate.py src/enterprise_rag/retrieval/contracts.py src/enterprise_rag/config/settings.py tests/unit/test_weaviate_transport.py
git commit -m "增加 Weaviate transport contract"
```

### Task 4: Collection registry、generation 构建与 checkpoint

**Files:**
- Create: `src/enterprise_rag/retrieval/generation.py`
- Modify: `src/enterprise_rag/retrieval/indexer.py`
- Test: `tests/unit/test_weaviate_indexer.py`
- Test: `tests/unit/test_weaviate_generation.py`

**Interfaces:**
- Produces `GenerationService.ensure_tenant_collection(session, tenant_id, schema_version)`、`start_generation(session, scope, profile, manifest_hash, expected_item_count, worker_lease)`、`record_checkpoint(session, generation_id, next_child_index)`、`validate_generation(session, generation_id, transport)`。
- Produces `WeaviateDenseIndexer.build_generation(session, *, generation_id, tenant_id, document_id, version_id, profile, parsed_object_key, batch_size)`。

- [x] **Step 1: Write the failing test**

固定 manifest 和 fake transport，锁定以下行为：

- manifest tenant/document/version 不匹配时立即失败；
- 只为 child 调用 embedding，Parent 文本和 locator 写入 Weaviate object；
- 每批成功后 checkpoint 更新；
- 第 2 批失败时 generation 保持 `BUILDING`，重试从 checkpoint 后继续；
- 同一 `(generation_id, chunk_id, index_profile)` 重复 upsert 不产生重复逻辑对象；
- dimension/model/manifest hash/chunk identity 不一致时不能转 `READY`。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_weaviate_indexer.py tests/unit/test_weaviate_generation.py -q`

Expected: FAIL because generation service 和 Weaviate indexer 尚不存在。

- [x] **Step 3: Write minimal implementation**

复用现有 `DenseIndexer` 的 manifest 解析和 `OllamaEmbeddingProvider` 调用，但把持久化目标换成 `WeaviateTransport.upsert_batch()`。每个 object 必须包含：

```python
{
    "tenant_id": tenant_id,
    "document_id": document_id,
    "version_id": version_id,
    "generation_id": generation_id,
    "index_profile": profile.key,
    "chunk_id": child.chunk_id,
    "parent_id": child.parent_id,
    "text": child.text,
    "parent_text": parent_text_by_id.get(child.parent_id),
    "heading_path": child.heading_path,
    "source_locators": child.source_locators,
}
```

构建前记录 expected item count、manifest hash、profile；构建后调用 `count_generation()` 与 `list_chunk_ids()` 做 scope/count/identity/model/dimension/schema 检查，通过后才把 generation 标记为 `READY`。实现 worker fencing：写入前校验 `build_attempt` 和 `worker_lease` 仍是当前值。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_weaviate_indexer.py tests/unit/test_weaviate_generation.py -q`

Expected: PASS，失败批次可恢复，旧 attempt 的写入被拒绝，完整性检查失败不会更新 active pointer。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/retrieval/generation.py src/enterprise_rag/retrieval/indexer.py tests/unit/test_weaviate_indexer.py tests/unit/test_weaviate_generation.py
git commit -m "实现 Weaviate generation 构建与 checkpoint"
```

### Task 5: Weaviate repository、active pointer 查询和 API 兼容

**Files:**
- Modify: `src/enterprise_rag/retrieval/repository.py`
- Modify: `src/enterprise_rag/retrieval/service.py`
- Modify: `src/enterprise_rag/api/app.py`
- Create: `tests/api/test_retrieval_weaviate_injection.py`
- Modify: `tests/api/test_retrieval_query_api.py`

**Interfaces:**
- Produces `WeaviateVectorRepository.search(session, tenant_id, version_id, query, top_k, index_profile)`。
- Repository 查询前必须从 PostgreSQL pointer 读取 active generation 和 tenant collection；Weaviate filter 必须同时带 `version_id`、`generation_id`、`index_profile`。

- [x] **Step 1: Write the failing test**

注入 fake PostgreSQL pointer repository 和 fake Weaviate transport，验证：

- API request/response schema 与现有 `/api/v1/retrieval/query` 完全相同；
- `BUILDING`/未发布 generation 不会调用 Weaviate search；
- tenant A 不会使用 tenant B collection；
- pointer 不存在时返回稳定 `retrieval_unavailable` 或 `evidence_not_found`，不回显数据库异常。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_retrieval_weaviate_injection.py tests/api/test_retrieval_query_api.py -q`

Expected: FAIL because Weaviate repository 和显式注入路径尚不存在。

- [x] **Step 3: Write minimal implementation**

在 `repository.py` 增加 Weaviate adapter，保留 `PostgresVectorRepository` 和现有构造方式。`create_app()` 新增可选 `weaviate_vector_repository`/`generation_service` 参数，仅放入 `app.state` 并由测试或显式部署配置启用；当未注入时继续使用 PostgreSQL repository。查询结果沿用现有 Parent 回溯、tenant/version 过滤和错误映射，不新增 API 字段。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_retrieval_weaviate_injection.py tests/api/test_retrieval_query_api.py -q`

Expected: PASS；既有 PostgreSQL API 测试保持通过，Weaviate 未发布 generation 不能被读到。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/retrieval/repository.py src/enterprise_rag/retrieval/service.py src/enterprise_rag/api/app.py tests/api/test_retrieval_weaviate_injection.py tests/api/test_retrieval_query_api.py
git commit -m "接入 Weaviate active generation 查询适配"
```

### Task 6: 发布、回滚、清理和历史回填命令

**Files:**
- Modify: `src/enterprise_rag/retrieval/generation.py`
- Modify: `src/enterprise_rag/db/publish.py`
- Create: `scripts/weaviate_backfill.py`
- Create: `tests/unit/test_weaviate_backfill.py`
- Modify: `tests/integration/test_weaviate_postgres.py`

**Interfaces:**
- Produces `backfill_versions()`、`retire_generation()` 和 `cleanup_generation()`；发布和回滚统一复用 Task 2 定义的 `publish_dense_generation()` 与 `rollback_dense_generation()`。
- `backfill_versions()` 按 PostgreSQL version 快照创建 generation，不自动更新 active pointer。

> 当前实现完成 service-level orchestration 和 contract；独立 `scripts/weaviate_backfill.py` CLI 暂缓，待真实 worker、manifest 枚举和凭据注入边界确定后再实现，避免提供无法执行的空壳命令。

- [x] **Step 1: Write the failing test**

写测试锁定：

- 历史回填只枚举 `READY` version 和目标 `index_profile`；
- 回填重启从 checkpoint 恢复；
- 回填完成只到 `READY`，不会自动 `ACTIVE`；
- 发布后旧 generation 进入 `RETIRED` 但仍可回滚；
- 清理只删除非 active、无运行任务且超过保留窗口的 generation；
- 删除失败记录清理任务，不影响 active 查询。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_weaviate_backfill.py tests/integration/test_weaviate_postgres.py -q`

Expected: FAIL because backfill、retire 和 cleanup orchestration 尚不存在。

- [x] **Step 3: Write minimal implementation**

脚本只接受显式 tenant/version/profile 参数或从 PostgreSQL 枚举，不接受用户传入 Weaviate URL/API key。每次批次记录安全的 generation、batch index、count 和状态；异常只输出类型化 safe message。发布函数先验证 generation 完整性，再在 PostgreSQL transaction 内更新 active/previous pointer；回滚只更新 pointer，不删除对象；清理调用 `delete_generation()`，失败保留 `cleanup_pending` 状态。

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_weaviate_backfill.py tests/integration/test_weaviate_postgres.py -q`

Expected: PASS；无 DSN 测试 skip，真实 PostgreSQL 下发布/回滚/清理状态可重复验证。

- [ ] **Step 5: Commit**

```bash
git add src/enterprise_rag/retrieval/generation.py src/enterprise_rag/db/publish.py scripts/weaviate_backfill.py tests/unit/test_weaviate_backfill.py tests/integration/test_weaviate_postgres.py
git commit -m "增加 Dense generation 回填发布回滚流程"
```

### Task 7: 真实 Weaviate smoke、文档同步和完整验证

**Files:**
- Create: `scripts/weaviate_smoke.py`
- Create: `artifacts/experiments/2026-09-16_weaviate-dense-smoke/README.md`
- Create: `artifacts/experiments/2026-09-16_weaviate-dense-smoke/metrics.json`
- Create: `artifacts/experiments/2026-09-16_weaviate-dense-smoke/conclusion.md`
- Modify: `docs/architecture.md`
- Modify: `docs/risk-register.md`
- Modify: `docs/roadmap.md`
- Modify: `docs/acceptance-matrix.md`
- Modify: `PROJECT_CONTEXT.md`
- Modify: `.planning/2026-09-09-enterpriseknowledgerag-development/task_plan.md`
- Modify: `.planning/2026-09-09-enterpriseknowledgerag-development/progress.md`
- Modify: `.planning/2026-09-09-enterpriseknowledgerag-development/findings.md`

**Interfaces:**
- Produces脱敏 smoke report，明确 collection 创建、generation 写入、完整性检查、发布、查询、回滚、清理的每一步状态。
- 不改变默认 API read path；如果真实 Weaviate 不可用，记录环境缺口，不伪造通过。

- [x] **Step 1: Write the failing test**

为 smoke report 写纯逻辑测试：

- report 不包含 DSN、密码、API key 或完整模型回答；
- `vector_backend`、`collection_strategy`、`generation_scope`、`active_generation` 和每阶段状态字段固定；
- 失败阶段不会被写成成功，旧 active 保持可读。

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_weaviate_smoke.py -q`

Expected: FAIL because smoke runner and report schema do not exist。

- [x] **Step 3: Write minimal implementation**

使用固定小 manifest、随机 generation id 和隔离 tenant collection 执行 smoke；只保存 count、dimension、model/profile、HTTP status class、generation status、rollback result 和 cleanup result。文档同步时严格区分：已实现、已验证、待实验、非目标；不能把真实 smoke 外推为生产容量、召回质量或 SLO。

- [x] **Step 4: Run test to verify it passes**

按以下顺序执行：

```bash
pytest tests/unit/test_weaviate_generation.py tests/unit/test_weaviate_transport.py tests/unit/test_weaviate_indexer.py tests/unit/test_weaviate_backfill.py tests/api/test_retrieval_weaviate_injection.py -q
pytest -q
python -m ruff check .
python -m compileall -q src tests scripts
docker compose config --quiet
git diff --check
```

若已由用户提供可用 Weaviate 开发环境，再运行：

```bash
python scripts/weaviate_smoke.py
```

Expected: 离线/contract/integration 证据通过；真实 smoke 只在依赖可用时产生通过记录，否则明确记录 skipped/blocked 原因。

- [ ] **Step 5: Commit**

```bash
git add scripts/weaviate_smoke.py tests/test_weaviate_smoke.py artifacts/experiments/2026-09-16_weaviate-dense-smoke docs/architecture.md docs/risk-register.md docs/roadmap.md docs/acceptance-matrix.md PROJECT_CONTEXT.md .planning/2026-09-09-enterpriseknowledgerag-development
git commit -m "完成 Weaviate Dense 迁移验证与文档同步"
```

## 验收标准

1. Migration 可从现有 `0002_embedding_records` 幂等升级到 `0003_weaviate_dense_generations`。
2. tenant collection registry 并发创建只产生一个稳定映射。
3. generation 按 `(tenant_id, version_id, index_profile)` 隔离，旧 attempt 不能覆盖新 attempt。
4. 批次重复写入幂等，部分失败可以从 checkpoint 恢复。
5. tenant/version/generation/model/dimension/manifest hash/chunk identity 任一不匹配都不能发布。
6. 发布事务失败时 active pointer 不变；回滚只切 pointer，不依赖重新写入 Weaviate。
7. 未发布 generation 永远不会进入查询结果；tenant A 不会读取 tenant B collection。
8. 默认 API 和 PostgreSQL repository 回归保持通过；Weaviate 只通过显式注入启用。
9. 所有新增代码、测试、实验产物和文档都不泄露凭据，且静态检查通过。

## 明确不做

本计划不安装或启用 `pgvector`，不实现 BM25F/RRF/reranker，不切换默认线上读路径，不新增 ACL/OIDC、Celery/RabbitMQ、Redis、OCR、上传/Git 同步或生产容量验证。
