# 检索流水线验证手册

## 测试层次

| 层次 | 验证对象 | 典型证据 |
|---|---|---|
| unit | ID、规范化、切块、去重、融合、token 预算 | 确定性输入输出断言 |
| contract | Document/Chunk/Candidate/Trace 字段与错误类型 | Schema 或协议测试 |
| integration | 事实源、对象存储、dense/sparse 索引、Reranker | 临时索引中的真实写入与查询 |
| failure | 部分批次、timeout、重复消息、取消、删除 | 状态、可见性和重放结果 |
| security | 通道过滤一致性、Parent 回溯、撤权后缓存 | 零越权结果和审计记录 |
| evaluation | Recall、ranking、context 与端到端贡献 | 固定数据集和版本化报告 |

## 先失败再实现

对新能力或 bug，先写最小测试并确认：

1. 测试失败而不是测试环境报错。
2. 失败原因正是缺失行为或原始 bug。
3. 测试断言业务结果，不只断言 mock 调用次数。
4. 最小实现通过后，相关集成与安全测试仍通过。

## 必测不变量

### 摄取

- 同一 source revision 重放两次，可见 document/version/chunk 集合不变。
- 内容变化或 chunking strategy 变化时生成新版本，旧版本不与新版本混合。
- 任一必需 embedding 批次失败，版本保持不可见。
- 发布切换前后的查询只看到完整旧版本或完整新版本。
- 删除与撤权后，所有查询通道、Parent 查找和缓存都无法返回对象。

### 召回与排序

- dense 与 sparse 使用同一 tenant、ACL、版本和发布状态约束。
- 候选融合不丢失原通道 rank/score，也不引入集合外对象。
- Reranker 只接收已授权候选，并且输出是输入的子集或重排。
- 查询改写有最大数量，并继承原请求 deadline。
- 变更 top-k 或 token budget 时，trace 能解释被选择和被排除的证据。

### 上下文与引用

- Parent 扩展重新授权；任何失败都从 evidence 集合移除并记录原因。
- 重复 chunk 或重叠 Parent 不重复占用 context budget。
- 版本冲突不会被静默合并成单一事实。
- 引用定位到实际进入 Prompt 的 document/version/chunk/source。

## 定量指标

不要先设通用阈值。先按查询类别建立 baseline，再由业务风险确定 gate。

| 维度 | 可选指标 | 诊断问题 |
|---|---|---|
| candidate recall | Recall@k、Hit Rate@k | 期望证据是否进入候选集 |
| ranking | MRR、nDCG@k、Precision@k | 相关证据是否排在前面 |
| channel contribution | dense-only、sparse-only、hybrid 消融 | 每个通道是否提供独立价值 |
| context quality | context precision/recall、重复率、冲突率 | 送入模型的 evidence 是否有效 |
| operations | p50/p95/p99、timeout rate、index lag | 质量收益是否值得延迟和成本 |
| safety | unauthorized hit count、stale-version hit count | 是否出现任何安全或版本泄漏 |

安全计数的目标必须是零；质量和延迟阈值由固定数据集、硬件和服务目标决定。

## 诊断顺序

1. 检查目标文档是否成功解析并发布。
2. 检查期望 chunk 是否存在且 lineage、tenant、ACL、version 正确。
3. 分别执行 metadata、sparse、dense 查询，定位是哪一通道 miss。
4. 检查融合前后 rank 和候选丢失原因。
5. 检查 Reranker 是否错误降权，或 Parent/context 阶段是否截断。
6. 最后检查生成模型；不要在检索证据缺失时直接调 Prompt。

## 验收报告

报告至少包含：

- 数据集、索引、Embedding、Reranker、chunking 和配置版本；
- 修改前后按类别分组的检索指标；
- latency、token、存储和外部调用变化；
- 失败与安全场景的测试结果；
- 一个可复现 trace 示例，能解释最终 evidence；
- 未覆盖文档类型、查询类型和基础设施故障。
