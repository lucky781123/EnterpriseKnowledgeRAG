# 检索流水线设计

## 契约优先

先画出阶段边界，再选择库或数据库。最小业务对象如下：

| 对象 | 必需信息 | 关键约束 |
|---|---|---|
| Document | tenant、source、title、visibility | 逻辑文档身份不随重新摄取变化 |
| Version | document、content hash、source revision、publish state | 查询只读取已发布版本 |
| Chunk | document、version、chunk、parent、text hash、position、metadata | ID 稳定、可回到原始证据 |
| Candidate | chunk identity、channel score、rank、authorization result | 分数保留通道归因 |
| Evidence | selected text、citation、version、source | 进入 Prompt 后仍能生成可信引用 |
| RetrievalTrace | 阶段、数量、耗时、失败、最终选择 | 不包含密钥和未脱敏原文 |

共享 Schema 位于 `../_shared/contracts/`。项目内部可以使用 Pydantic、dataclass 或其他类型，但跨模块边界应保留同等语义。

## 摄取与发布

### 稳定身份

- `document_id` 来自 tenant 内稳定的 source identity，不来自文件名或数据库自增顺序。
- `version_id` 应关联规范化内容 hash 或可信 source revision。
- `chunk_id` 应关联 version、结构锚点、文本 hash 和 chunking strategy version。
- 不要只用字符 offset 作为永久身份；上游插入一段文本会导致大范围漂移。

### 处理顺序

1. 验证媒体类型、大小、编码、恶意载荷和解析能力。
2. 保存原始对象与 content hash，建立可重放入口。
3. 解析为带标题、段落、列表、代码、表格等结构的中间表示。
4. 做可解释的规范化；保留原文位置映射，避免不可逆清洗。
5. 按文档结构切块，在语义完整性、召回粒度和 token 成本间取舍。
6. 生成 embedding、sparse fields 和检索 metadata；每个派生物携带完整 lineage 与 ACL metadata。
7. 写入隔离的未发布版本，校验 chunk 数、向量维度、索引计数、重复率和必需 metadata。
8. 通过事实源一次性切换可见版本；旧版本按保留策略清理，不让查询看到中间状态。

### 幂等与删除

- 相同 idempotency key 或相同 source revision 重放时，最终可见状态相同。
- 部分写入必须可识别、可清理、不可查询；不能靠“通常按顺序成功”保证一致性。
- 撤权或删除先在权威数据层取消可见性，再异步清理向量、倒排、缓存、引用快照和原始派生物。
- 清理任务必须可重试并能证明没有遗留可检索对象。

## 查询流水线

### 1. 查询理解

只做能够通过评测证明价值的规范化、拆分、改写或 HyDE。保留原问题和每次转换，限制派生查询数量，并继承原始 deadline。

### 2. 授权过滤

从安全层接收可信 `AuthorizationContext` 或已编译过滤表达式。dense、sparse、metadata、Parent 查询和缓存回源必须使用等价约束。检索 Skill 不负责从自然语言猜测权限。

### 3. 召回策略

| 情况 | 优先策略 | 原因 |
|---|---|---|
| 错误码、API、精确标识符 | sparse 或 hybrid | 词法匹配通常更稳定 |
| 同义表达、概念问题 | dense 或 hybrid | 需要语义近邻 |
| 结构化字段和严格范围 | metadata filter 后检索 | 先缩小合法集合 |
| 查询类型混合或未知 | hybrid，分别观测通道贡献 | 降低单通道偏差 |

不要默认 hybrid 一定更好。保留单通道消融结果；如果一个通道持续无贡献，应简化系统。

### 4. 融合与重排

- 融合前保留各通道 rank 和 raw score，不直接比较不同分布的原始分数。
- RRF 等 rank-based 方法适合缺少可靠 score calibration 的通道。
- Reranker 输入应是授权后、去除明显重复、数量有上限的候选。
- Reranker 超时或协议错误必须作为结构化阶段状态返回，是否跳过由 degradation Skill 决定。

### 5. Parent 扩展与上下文组装

- 扩展 Parent 或相邻块后重新验证 tenant、ACL、版本和发布状态。
- 以证据单元去重，避免同一段落因多路召回重复占用 token。
- 明确处理版本冲突：选择固定版本、并列展示冲突，或拒绝给出确定结论。
- context packing 记录 token 预算、证据顺序、截断位置和被排除原因。
- 引用使用实际送入模型的 evidence，而不是早期候选列表。

## 失败分类

| 阶段 | 示例 | 输出要求 |
|---|---|---|
| parse | 不支持格式、结构恢复失败 | 文档/页级失败范围、是否可隔离 |
| embedding | timeout、维度错误、部分批次失败 | 失败批次、重试性、版本不可发布 |
| index | 写入失败、计数不一致 | 未发布状态、清理或重放信息 |
| retrieve | 后端不可用、查询语法错误 | 通道、错误类型、剩余预算 |
| rerank | timeout、模型协议错误 | 原候选仍可追踪，不静默吞错 |
| context | token 超限、冲突无法处理 | 被排除证据和最终 evidence 状态 |

检索层报告事实，不在内部执行未经声明的跨 Provider 或跨检索 fallback。

## 调优顺序

1. 固定有代表性的 query set、权限上下文、索引版本和评测版本。
2. 建立 sparse、dense、hybrid 的独立 baseline。
3. 检查 miss taxonomy：解析缺失、切块错误、过滤过严、召回不足、融合错误、重排错误、预算截断。
4. 一次只改变一个主要变量，并保存 trace 与成本变化。
5. 只有检索指标改善且端到端没有安全、延迟或答案质量回归时才接受变更。

## 反模式

- 用更大的 top-k 掩盖错误切块或错误 metadata。
- 只看最终答案，无法判断改进来自召回还是模型猜测。
- 在索引对象缺少版本、tenant 或 ACL 时依赖应用层事后补救。
- 将原始相似度当成跨通道可直接比较的概率。
- 更新 embedding 或 chunking 策略却不改变 index/strategy version。
