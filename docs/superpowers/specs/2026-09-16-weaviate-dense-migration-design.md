# Weaviate Dense 迁移设计

## 状态

- 日期：2026-09-18
- 状态：设计、实现与真实 transport smoke 已完成；方案 C 全链路上传到引用 smoke 待专用配置
- 前置基线：当前 P0 使用 PostgreSQL JSON + 应用层 cosine 的 Dense 检索路径
- 目标后端：Weaviate Dense collection

本文记录已批准的 Weaviate Dense 迁移架构及当前实现边界。对应代码已经实现，真实 Weaviate `1.27.10` transport smoke 已验证 collection、批量写入、完整性、nearVector 查询和删除；这不代表生产容量、跨系统发布回滚或方案 C 全链路上传到引用 smoke 已完成。

当前状态分层如下：

- **已实现：** tenant 独立 collection、generation scope、PostgreSQL active/previous pointer、批量 checkpoint、完整性校验、回填、retire、cleanup 和显式 repository 注入。
- **已验证：** 真实 Weaviate transport 的建 collection、批量写入、完整性校验、nearVector 查询和删除。
- **待实验：** 方案 C 在专用 PostgreSQL/Weaviate/Ollama/BGE/LM Studio 配置下的上传到引用 smoke，以及真实环境中的跨系统发布/回滚联调。
- **非目标：** 本文不冻结 Sparse Generation 的通用状态机、不切换默认旧 retrieval API 的 PostgreSQL 读取路径，也不提供生产容量结论。

## 目标

在保持现有 `POST /api/v1/retrieval/query` 契约不变的前提下，将 Dense 向量从当前 PostgreSQL 演示后端迁移到 Weaviate，同时保证：

- PostgreSQL 继续作为 tenant、document、version、ACL、artifact 状态和发布事实的唯一事实源；
- Weaviate 只保存可重建的 Dense 派生数据；
- 每个 tenant 使用独立 Weaviate collection；
- 每个 `(tenant_id, version_id, index_profile)` 使用独立 immutable generation；
- 未完整构建、未通过校验或未发布的 generation 不可进入正式查询路径；
- 新旧 generation 可以安全切换和回滚；
- Weaviate 暂时不可用时，旧的 active generation 不被破坏。

## 已确认方案

### Collection 布局：方案 6A

每个 tenant 拥有一个独立 Dense collection。collection 表示 tenant 的物理数据边界，不表示当前可读版本。

```text
PostgreSQL
  tenant_id -> collection_name

Weaviate
  tenant_a_dense
    generation=g-a-001
    generation=g-a-002
  tenant_b_dense
    generation=g-b-001
```

本方案不依赖 Weaviate 原生 multi-tenancy 作为主要隔离机制。ACL、发布事实和请求级授权仍由 PostgreSQL 负责；collection 隔离不能替代授权校验。

### Generation 作用域

generation 的作用域固定为：

```text
(tenant_id, version_id, index_profile)
```

`index_profile` 至少描述：

- embedding model；
- embedding dimension；
- chunking/preprocessing version；
- Weaviate schema version。

这样可以只重建一个 tenant 的一个 document version，不必复制整个 tenant 的向量集合。

## PostgreSQL 事实模型

PostgreSQL 需要保存 tenant 与 collection 的稳定映射，以及每次 generation 构建和发布的事实。字段语义建议如下：

```text
tenant_id
version_id
index_profile
generation_id
collection_name
manifest_hash
model_name
dimension
expected_item_count
actual_item_count
status
active_generation
checkpoint
build_attempt
created_at
published_at
retired_at
```

建议的 generation 状态为：

```text
BUILDING -> READY -> ACTIVE -> RETIRED
        \-> FAILED
```

状态含义：

- `BUILDING`：允许继续写入或从 checkpoint 恢复；不可查询；
- `READY`：写入完成且完整性检查通过；仍不可正式查询；
- `ACTIVE`：已通过 PostgreSQL 发布事务，允许正式查询；
- `RETIRED`：不再是 active，但在保留窗口内可用于回滚；
- `FAILED`：本次构建不可继续作为当前 generation 发布，可创建新 attempt 或新 generation。

`active_generation` 是 PostgreSQL 发布指针。查询路径不得根据 Weaviate 中“看起来存在”的对象自行推断 active generation。

## Collection 生命周期

### 创建

采用惰性创建：首次为 tenant 构建 Dense generation 时创建 collection。

创建流程：

1. PostgreSQL 先登记 tenant 到 collection 的映射和创建状态；
2. 通过唯一约束或行锁避免并发创建同一 tenant collection；
3. 创建固定 schema，至少包含 generation、version、chunk identity、文本和来源定位字段；
4. collection 创建成功后才允许 generation 写入；
5. 创建失败时 generation 不得进入 `READY` 或 `ACTIVE`。

collection 名称不能直接无约束拼接原始 `tenant_id`。应使用稳定编码或哈希生成，并将映射保存在 PostgreSQL，避免非法字符、长度限制和敏感标识泄露。

### Schema 演进

generation 可以解决数据版本切换，但不能自动解决 collection schema 的不兼容变更。后续若 schema 发生不兼容变化，需要执行受控的 tenant collection 迁移或重建；该问题不在本切片的 generation 发布流程中伪装解决。

## 双写语义

采用 PostgreSQL 事实先行、Weaviate 异步构建和补偿：

```text
PostgreSQL 记录事实
        |
        v
创建 generation 并异步写入 Weaviate
        |
        v
完整性检查
        |
        v
PostgreSQL 发布 active_generation
```

PostgreSQL 与 Weaviate 之间不存在跨系统原子事务，因此不宣称“双写原子成功”。具体保证如下：

- PostgreSQL 的 document、version、artifact 和发布状态是事实；
- Weaviate 写入失败不会伪造 PostgreSQL 的索引完成状态；
- 未完成 generation 不进入查询路径；
- 当前 active generation 保持可读；
- 写入任务按 generation 和 chunk identity 幂等重试；
- PostgreSQL 成功而 Weaviate 未完成时，由 checkpoint 或补偿任务继续处理。

## 历史回填

历史回填只处理满足条件的 PostgreSQL version，例如已完成解析且允许建立 embedding 的版本。

流程：

1. 枚举符合条件的 version；
2. 为每个 `(tenant_id, version_id, index_profile)` 创建 generation；
3. 从不可变 `ParsedManifest` 读取 child chunk；
4. 批量生成 embedding 并写入 tenant collection；
5. 每批保存 checkpoint；
6. 进程重启时从 checkpoint 恢复；
7. 完成后执行完整性检查；
8. 检查通过后标记 `READY`；
9. 只有显式发布才更新 `active_generation`。

批次幂等键建议为：

```text
(generation_id, chunk_id, index_profile)
```

同一 generation 重试不得创建重复的有效 chunk 记录，也不得把不同 generation 的记录互相覆盖。

## 完整性检查

generation 转为 `READY` 前至少检查：

- tenant scope 正确；
- version scope 正确；
- generation scope 正确；
- 实际 chunk 数量与预期一致；
- `manifest_hash` 一致；
- embedding model 一致；
- embedding dimension 一致；
- chunk identity 集合完整；
- collection schema version 兼容。

任何检查失败都禁止发布。数量检查通过也不能替代 chunk identity、model、dimension 和 manifest hash 校验。

## 发布、读切换与回滚

### 发布

发布只允许对 `READY` generation 执行：

1. PostgreSQL 事务锁定对应 tenant/version 的发布记录；
2. 校验 version 状态、artifact 状态和 generation 完整性；
3. 更新 `active_generation`；
4. 提交事务。

发布事务失败时，active 指针保持原值。

### 查询

查询流程保持现有 API 契约不变：

1. PostgreSQL 校验 tenant、version、ACL 和发布状态；
2. 读取 PostgreSQL 的 `active_generation`；
3. 定位 tenant 对应 collection；
4. 使用 `version_id + generation_id` 过滤 Weaviate；
5. 对返回结果执行必要的授权复核和 Parent 回溯。

查询不得读取：

- `BUILDING` generation；
- `READY` 但尚未发布的 generation；
- 其他 tenant collection；
- PostgreSQL 当前发布事实之外的版本。

### 回滚

回滚只修改 PostgreSQL active 指针，切回上一个已验证 generation：

- 不要求重新写入 Weaviate；
- 不删除当前 generation；
- 回滚完成后旧 generation 恢复正式读路径；
- 删除或清理由后台任务异步执行。

## 并发与旧任务隔离

同一个 `(tenant_id, version_id, index_profile)` 默认只允许一个未终止的构建 generation。不同 version 可以并行构建，但每条写入都必须带完整 `generation_id`。

为防止旧 worker 在重试或接管后继续写入，构建任务需要 fencing 信息，例如：

```text
generation_id
build_attempt
worker_lease
```

新 attempt 接管后，旧 attempt 的写入必须被拒绝或视为无效。

## 失败处理

| 故障 | 处理 | 正式读路径 |
|---|---|---|
| collection 创建失败 | generation 标记 `FAILED`，允许重试 | 保持旧 active |
| Weaviate 批次失败 | 按 chunk 幂等重试，超限后 `FAILED` | 保持旧 active |
| 重复批次写入 | 幂等 upsert | 不受影响 |
| dimension 不一致 | 构建前或批次校验失败 | 禁止发布 |
| model/profile 不一致 | 完整性检查失败 | 禁止发布 |
| chunk 数量或 identity 不一致 | 保持 `BUILDING/FAILED` | 保持旧 active |
| manifest hash 不一致 | 禁止转为 `READY` | 保持旧 active |
| PostgreSQL 发布事务失败 | 不更新 active 指针 | 保持旧 active |
| 发布后查询异常 | 回退 active 指针 | 切回旧 generation |
| 清理旧 generation 失败 | 记录清理任务并重试 | 不影响查询 |
| worker 崩溃 | checkpoint 恢复或新 attempt 接管 | 保持旧 active |

任何 fallback 都只能指向上一个已验证的 active generation，不能把“部分完成的最新 generation”当作 fallback。

## 清理与保留

旧 generation 只有同时满足以下条件才允许清理：

- 不是 active generation；
- 没有运行中的构建、回填或重试任务；
- 超过保留窗口；
- 没有待执行回滚或审计保留要求。

清理失败不影响正式查询，只记录孤立 generation 和清理任务状态。

## 验证计划

### 单元和状态测试

- collection name 映射稳定且不会泄露原始 tenant 标识；
- generation 状态转换满足合法边界；
- generation 和 chunk 幂等键稳定；
- 旧 attempt 不能覆盖新 attempt；
- 未发布 generation 不可查询。

### Repository/事务集成测试

- PostgreSQL 发布事务成功后 active 指针切换；
- 发布事务失败时 active 指针不变；
- 回滚只修改指针且可恢复旧 generation；
- tenant/version/ACL 过滤边界保持正确；
- 并发 collection 创建只产生一个有效 registry 映射。

### Weaviate contract tests

- 批量 upsert 和重复 upsert；
- 部分批次失败与 checkpoint 恢复；
- dimension/model/schema 校验；
- 删除和清理失败的可重试语义；
- collection 不可用时旧 active generation 仍可读。

### 真实 smoke

最后再使用固定小数据集验证真实 Weaviate：

- collection 创建；
- generation 批量写入；
- 完整性检查；
- 发布和查询；
- 回滚；
- 清理。

真实 smoke 只能证明当前开发环境的协议和流程可用，不代表生产容量、召回质量、SLO 或高可用。

## 非目标

本设计不实现或不宣称完成：

- BM25F、RRF、reranker；
- ACL/OIDC 请求鉴权闭环；
- Celery/RabbitMQ、Redis 或生产队列拓扑；
- OCR、上传/Git 同步；
- 在线 Provider；
- 生产容量、高可用、检索质量或 SLO 结论；
- 立即切换现有线上读取路径；
- 用 Weaviate 替代 PostgreSQL 作为权限或发布事实源。

## 待实验问题

- 真实 Weaviate collection 命名约束和 schema API；
- 单 tenant collection 数量上限与运维成本；
- 批量 upsert 的批次大小、超时和重试预算；
- collection 创建、删除和 schema 升级的真实行为；
- 小数据集上的完整性检查成本和查询延迟；
- 旧 generation 保留窗口和孤立数据清理策略。
