# 本地最小 P0 产品闭环设计

## 状态

- 日期：2026-09-17
- 状态：用户已确认，进入实施
- 已确认方案：方案 C，本地同步产品闭环
- 基线：真实摄取基础、Weaviate Dense generation、BM25F transport、RRF 质量实验、BGE Reranker Adapter 和 F2-G 摘录问答均已有独立实现或验证证据

本文描述新的本地 P0 验收口径。它覆盖上传、同步摄取、索引发布、开发身份 ACL、混合检索、问答和引用复核，但不把开发身份、单进程同步执行或本机依赖冒充生产级认证、异步队列和高可用能力。

## 目标

提供一条可以由用户直接操作、具有真实依赖和明确授权边界的本地产品主链：

```text
开发身份 token
    |
    v
上传 Markdown / HTML / 受控文本 PDF
    |
    v
同步校验、解析、Parent/Child chunk
    |
    v
Ollama Dense Embedding + BM25F 字段投影
    |
    v
Weaviate 隔离 generation 写入与完整性检查
    |
    v
PostgreSQL 原子发布文档版本和 active generation
    |
    v
ACL 过滤 -> Dense + BM25F -> RRF -> BGE Reranker
    |
    v
Parent 回溯与再次授权
    |
    v
LM Studio 摘录问答、引用 identity 校验与可访问性复核
```

最终验收场景是：两个开发身份属于不同 tenant 或具有不同文档权限；身份 A 上传并发布文档后可以得到带引用回答，身份 B 无法通过查询、资源 ID、Parent 回溯或引用接口观察该文档。

## 调整后的 P0 边界

### 实现

- `multipart/form-data` 文档上传 API；
- Markdown、HTML 和受控文本 PDF 的同步摄取；
- 原文、parsed manifest、Parent/Child lineage 和稳定 ID；
- Ollama Embedding 与 Weaviate Dense generation；
- BM25F `title`、`section_title`、`api_symbols`、`body` 字段投影；
- Dense 与 BM25F 共用 generation scope 的完整性检查；
- PostgreSQL 内文档版本、索引 generation 和发布指针的一致切换；
- 开发环境签名 token、tenant/actor/role 上下文和文档级 allow ACL；
- Dense+BM25F 双路召回、确定性 RRF、BGE Reranker、Parent 去重和回溯；
- 检索证据转换为现有 F2-G evidence，并继续使用 LM Studio、调度器和引用校验；
- 文档、版本、任务和索引状态查询；
- 最小 ACL 授予与撤销接口；
- 精简本地工作台中的上传、状态和检索问答入口；
- Compose 中 PostgreSQL migration、Weaviate、API 和本地对象存储路径的开发编排。

### 后置

- Keycloak、OIDC discovery、JWKS 轮换和真实成员关系同步；
- Celery、RabbitMQ、Redis、分布式 Worker 和后台补偿调度；
- Git 同步、Webhook、增量删除和仓库凭据管理；
- OCR、复杂表格、复杂版面恢复；
- Minikube、生产容量、高可用、备份恢复、RPO/RTO；
- 将 `cjk_bigram` 冻结为默认中文 profile；
- 在线 Provider 和生产 fallback 图。

## 核心设计决策

### 1. 上传与同步执行

新增产品上传接口：

```text
POST /api/v1/documents
Authorization: Bearer <development-token>
Content-Type: multipart/form-data

file=<binary>
source_id=<tenant 内稳定来源 ID>
title=<文档标题>
idempotency_key=<调用方重试键>
```

请求体不接受 `tenant_id`、`actor_id`、Provider endpoint、模型名称或密钥。tenant、actor 和 roles 只能来自已经验证的开发 token。

同步执行顺序固定为：

1. 验证开发 token 和上传角色；
2. 校验文件名、MIME、大小、编码和解析能力；
3. 调用现有 `IngestionService` 保存原文、解析并生成 manifest；
4. 为上传者创建或确认文档 `READ` ACL；
5. 从不可变 parsed manifest 读取 child chunk；
6. 创建或复用本次索引 generation；
7. 批量生成 Dense embedding 并写入 Dense collection；
8. 投影 BM25F 字段并写入 Sparse collection；
9. 分别检查数量、chunk identity、tenant、version、generation 和 profile；
10. 只有两路都完整时，将 `EMBEDDING`、`SPARSE` artifact 标记为 `READY`；
11. 在一个 PostgreSQL 事务内发布文档版本和 active generation；
12. 返回文档、版本、摄取任务、generation 和发布状态。

同步请求使用服务端固定总预算。客户端断开会取消尚未完成的 HTTP task，但不能假装已经写入 Weaviate 的对象自动回滚；未发布 generation 保持不可查询，重复使用同一 idempotency key 可以安全继续或重建。

### 2. 混合 generation

为减少对已经验证的 Dense generation 状态机进行大范围重构，本地 P0 继续使用现有 `DenseGenerationModel` 和 `DenseGenerationPointerModel` 作为发布锚点。名称保留历史兼容性，但发布条件扩展为“Dense 与 Sparse 派生物均完整”。

每个 generation 同时对应：

```text
(tenant_id, document_id, version_id, generation_id, index_profile_key)
```

Dense 与 Sparse 使用各自固定 schema 的 tenant collection，但所有对象携带相同的 `tenant_id`、`document_id`、`version_id`、`generation_id`、`chunk_id` 和 Parent lineage。Sparse collection 名称由 tenant 的稳定哈希映射派生，不能直接暴露原始 tenant 标识。

默认 Sparse profile 固定为：

```text
tokenization = word
preprocessing = identity
title = 2.0
section_title = 2.0
api_symbols = 3.0
body = 1.0
```

`cjk_bigram` 只保留为显式实验配置。人工确认 frozen 数据集完成前，产品默认值不得自动切换。

### 3. 发布一致性

PostgreSQL 是唯一发布事实源。Weaviate 不参与跨系统事务，因此设计不宣称 PostgreSQL 与 Weaviate 原子写入。

发布前必须满足：

- `RAW`、`PARSED`、`EMBEDDING`、`SPARSE` 全部为 `READY`；
- generation 状态为 `READY`；
- Dense 与 Sparse 实际 item 数均等于 parsed manifest child 数；
- 两路 chunk identity 集合一致；
- model、dimension、schema、chunking 和 sparse profile 与 generation 记录一致；
- 文档 ACL 至少包含上传者的 `READ` 授权。

最终 PostgreSQL 发布事务按固定顺序锁定 document、version、generation pointer，并完成：

1. 当前版本变为 `PUBLISHED`；
2. 旧版本按既有规则变为 `SUPERSEDED`；
3. `documents.current_version_id` 指向新版本；
4. generation pointer 的 active/previous 完成切换；
5. generation 状态变为 `ACTIVE`。

任一步失败都回滚 PostgreSQL 事实，旧版本和旧 generation 继续可读。新 generation 即使在 Weaviate 中存在，也因没有 active pointer 而不可进入查询路径。

### 4. 开发身份

本地 P0 使用 HMAC 签名的短期 Bearer token，不接受客户端自报 tenant Header。token 只在 `APP_ENV=development` 且显式启用开发认证时生效。

最小 claims：

```json
{
  "sub": "user-alice",
  "tenant_id": "tenant-a",
  "roles": ["reader", "editor"],
  "iat": 1789600000,
  "exp": 1789603600,
  "version": 1
}
```

实现使用 Python 标准库的 URL-safe Base64、JSON、HMAC-SHA256 和 `hmac.compare_digest()`，避免为本地开发 token 引入完整 OIDC SDK。密钥只从 `DEV_AUTH_SECRET` 读取，不写入代码、日志、实验产物或 Git。

提供本地 CLI 生成 token，但 CLI 不能绕过签名、过期时间或字段校验。生产环境或非 development 环境若启用该模式，应用启动应失败。

### 5. 角色和 ACL

本地 P0 角色：

| 角色 | 权限 |
|---|---|
| `reader` | 查询自己具有 `READ` ACL 的已发布文档 |
| `editor` | 上传文档；上传后自动获得该文档 `READ` ACL |
| `admin` | 上传、查询，并授予或撤销同 tenant 内文档 `READ` ACL |

授权顺序：

1. 验证 token 签名、过期时间和 claims；
2. 从 token 建立不可变 `RequestContext`；
3. 检查操作角色；
4. PostgreSQL 检查 tenant、文档当前版本和文档 ACL；
5. 只为授权文档解析 active generation；
6. Dense 和 Sparse 使用同一授权 scope；
7. RRF/Reranker 不得引入 scope 外 candidate；
8. Parent 回溯、evidence 打包和引用返回前再次查询 PostgreSQL 授权事实。

授权上下文缺失、签名错误、过期、tenant 不匹配、ACL 查询失败或 policy 无法解析时一律 fail closed。对外使用 `404` 隐藏其他 tenant 资源是否存在；内部日志只记录稳定拒绝原因和资源 identity，不记录 token 或正文。

最小管理接口：

```text
PUT    /api/v1/documents/{document_id}/acl/{principal_type}/{principal_id}
DELETE /api/v1/documents/{document_id}/acl/{principal_type}/{principal_id}
```

只有同 tenant 的 `admin` 可以操作。撤权提交后立即影响 PostgreSQL 授权决策，即使 Weaviate 中对象尚未清理也不能再次返回。

### 6. 产品查询契约

保留现有 `/api/v1/retrieval/query` 作为历史演示和兼容入口，但新增默认产品接口：

```text
POST /api/v1/rag/query
Authorization: Bearer <development-token>

{
  "question": "如何处理重复提交？",
  "top_k": 5,
  "candidate_k": 20,
  "max_tokens": 512,
  "timeout_ms": 120000
}
```

产品请求不接受 `tenant_id`、`version_id`、evidence、base URL、API key 或任意模型 endpoint。

服务端查询步骤：

1. 从可信身份取得 tenant、actor 和 roles；
2. PostgreSQL 列出调用者有权读取的当前发布文档及 active generation；
3. 对原问题只生成一次 embedding；
4. 对授权 scope 执行 Dense 和 BM25F 召回；
5. 保留每个 candidate 的通道 rank 和 score；
6. 使用确定性 RRF 融合，默认 `rrf_k=60`；
7. 按 chunk identity 去重后，将有界候选交给 BGE Reranker；
8. 选择最终 child，回溯 Parent 并重新检查 tenant、当前版本和 ACL；
9. 按 Parent identity 去重并执行上下文预算；
10. 转换为 F2-G evidence，调用 LM Studio；
11. 校验引用 identity、quote 范围和当前 ACL；
12. 返回答案、引用以及不含敏感正文的 retrieval trace 摘要。

Reranker 是产品主链必需阶段。未配置、加载失败或执行超时时，本地 P0 返回明确 `reranker_unavailable`，不把未重排结果伪装成正常模式。后续若要允许降级，需要单独定义并验证 fallback 政策。

### 7. 引用与资源访问

引用最少携带：

```text
tenant_id
document_id
version_id
chunk_id
parent_id
source locator
```

对外响应不直接暴露 tenant 内部 collection、generation worker lease 或对象存储 key。引用详情接口按当前身份重新检查 tenant、文档当前版本和 ACL；仅在授权通过后返回标题、locator 和允许展示的原文片段。

文档内容中的“忽略系统指令”“显示其他文档”“调用删除接口”等文本始终作为 evidence data，不改变身份、授权、工具能力或系统 Prompt。

### 8. 状态和重试

上传成功返回 HTTP `201`。相同 tenant 与 idempotency key、相同内容重复请求返回同一文档/version 事实，并在索引尚未发布时继续安全构建；相同幂等键但内容不同返回 `409`。

失败分类：

| 阶段 | 对外状态 | 事实处理 |
|---|---|---|
| token/role/ACL | `401/403/404` | 不读取或写入业务资源 |
| 输入校验/解析 | `422` | 使用既有 ingestion 失败状态和安全原因 |
| Embedding | `503` | `EMBEDDING=FAILED`，generation 不发布 |
| Dense/Sparse 写入 | `503` | 对应 artifact 失败或 generation `FAILED` |
| 完整性检查 | `409/503` | generation 不进入 `READY` |
| PostgreSQL 发布 | `503` | 事务回滚，旧 active 保持不变 |
| 无授权证据 | `404` | 不调用 LLM |
| Reranker | `503` | 不把 base rank 冒充正常结果 |
| LM Studio | 既有 Provider 映射 | 保留授权证据边界，不切换为无证据回答 |

本地 P0 不自动无限重试。重复上传和显式索引重试必须使用稳定 idempotency key，并始终受原 tenant 与 ACL 约束。

## 组件边界

建议新增或扩展以下聚焦组件：

- `security/context.py`：不可变开发身份与请求上下文；
- `security/dev_token.py`：签名、验证和过期检查；
- `security/authorization.py`：角色和 PostgreSQL ACL 决策；
- `ingestion/product_service.py`：同步编排摄取、Dense、Sparse 和发布；
- `retrieval/hybrid.py`：双路候选、RRF、Reranker 和 Parent 选择；
- `retrieval/scope.py`：从 PostgreSQL 解析授权的 active generation；
- `api/product_schemas.py`：上传响应、产品查询、ACL 和 trace schema；
- `api/app.py`：只负责 HTTP 映射和依赖注入，不内嵌新的索引或 ACL 算法；
- `workbench/app.py`：调用产品 API，不复制领域逻辑。

已有 `IngestionService`、`GenerationService`、`WeaviateVectorRepository`、`WeaviateBM25FTransport`、`SparseFieldProjector`、`SyncRerankerAdapter`、`ExcerptAnswerService` 和 Provider scheduler 必须优先复用。

## 配置

新增配置保持显式且默认 fail closed：

```text
DEV_AUTH_ENABLED=false
DEV_AUTH_SECRET=<至少 32 字节随机值>
DEV_AUTH_TOKEN_TTL_SECONDS=3600
OBJECT_STORAGE_ROOT=/data/objects
WEAVIATE_ENABLED=true
SPARSE_TOKENIZATION=word
SPARSE_PREPROCESSING=identity
RRF_K=60
RETRIEVAL_CANDIDATE_K=20
RERANKER_ENABLED=true
RERANKER_MODEL=<本地 BGE 模型名称或路径>
```

当产品路由启用时，PostgreSQL、Weaviate、Ollama、Reranker、LM Studio、对象存储和开发身份配置均必须进入 readiness。基础依赖未就绪时 API 保持 live，但产品 readiness 返回 not ready。

## 工作台

现有精简 Gradio 工作台扩展为三个操作区：

1. 输入开发 token 并上传文档；
2. 查看返回的文档/version/generation 状态；
3. 输入问题并展示回答、引用和安全的检索阶段摘要。

工作台只调用 FastAPI 产品接口，不直接访问 PostgreSQL、Weaviate、Ollama 或 LM Studio。token 只保存在当前浏览器会话内，不写入文件和日志。

## 测试与验收

### TDD 单元测试

- token 签名、篡改、过期、错误 secret 和非法 claims；
- 角色矩阵和 ACL allow/deny；
- Sparse profile 固定与 `cjk_bigram` 非默认；
- Dense/Sparse identity 集合一致性；
- RRF 通道归因、去重和稳定排序；
- Reranker 输入上限与结果映射；
- 引用 identity 和 Parent 去重。

### API 测试

- 上传者可以上传并自动获得 ACL；
- reader 不能上传，非 admin 不能授权；
- 请求体不能注入 tenant、version、evidence、endpoint 或密钥；
- token tenant A 无法读取或管理 tenant B；
- IDOR 返回不泄露资源存在性的稳定响应；
- 无授权证据时不调用 Reranker 和 LLM；
- 上传重复、幂等冲突和失败重试语义；
- 产品查询返回 `context_source=retrieved` 和有效引用。

### Repository/集成测试

- PostgreSQL ACL 与当前发布版本共同限制 scope；
- Dense 与 Sparse 对相同 tenant/version/generation 产生相同 chunk identity；
- 任一路不完整时发布被拒绝；
- PostgreSQL 发布失败时旧 version/generation 保持 active；
- 撤权后即使索引对象仍存在也返回零证据；
- Parent 回溯和引用访问重复执行授权；
- 两个 tenant 使用相同文本和 query 时结果不串租户。

### 本地真实 smoke

使用专用开发 PostgreSQL、Weaviate `1.27.10`、Ollama Embedding、进程内 BGE Reranker 和 LM Studio：

1. 生成 tenant A 的 editor/admin token 和 tenant B 的 reader token；
2. tenant A 上传固定 Markdown；
3. 检查四类 artifact、Dense/Sparse item 数和 active generation；
4. tenant A 查询并得到带有效引用的回答；
5. tenant B 使用相同问题得到零越权结果；
6. 撤销 tenant A reader ACL 后再次查询，结果立即被拒绝；
7. 重新授权后验证可恢复；
8. 清理实验 generation 和临时数据库，不删除持久化 volume。

真实 smoke 只证明当前本机协议和最小产品流程，不代表生产容量、企业真实质量或高可用。

## P0 完成标准

只有同时满足以下条件，才将“本地最小 P0 产品闭环”标记为完成：

- 上传 API、同步摄取、Dense/BM25F 构建和发布均有自动化测试；
- 默认产品查询实际执行 Dense+BM25F+RRF+BGE Reranker；
- tenant 和文档 ACL 在召回前、Parent 回溯和引用返回前均执行；
- 越权测试的 document、chunk、Parent 和 citation 命中均为零；
- 无授权证据时不会调用 LLM；
- 本地真实依赖 smoke 完成一次上传到引用回答；
- Compose 和工作台具有可复现启动步骤；
- 测试、Ruff、compileall、Compose config、Skill validator 和 diff check 通过；
- 项目文档严格标注开发 token、同步执行和单机验证边界；
- Keycloak、Celery/RabbitMQ、Redis 和生产能力仍明确为后置，不写成已完成。

## 需要用户参与的真实环境事项

实施可以先使用 Fake/contract 完成绝大部分代码和自动化测试。最终真实 smoke 前，需要用户在本机确认或执行：

- Docker Desktop 与现有专用 PostgreSQL 可用；
- `.env` 中的 `POSTGRES_DSN` 仍指向允许创建临时数据库的开发实例；
- Ollama 中目标 Embedding 模型可运行；
- LM Studio 已加载目标 LLM 并开放本地服务；
- BGE Reranker 模型已存在或允许下载到本机模型缓存；
- 生成并保管至少 32 字节的 `DEV_AUTH_SECRET`，不得提交到 Git。

上述事项只在真实 smoke 阶段需要，不阻塞先完成代码、Fake 测试和离线契约验证。
