---
name: rag-degradation-and-fallback
description: Use when designing, implementing, testing, or reviewing RAG behavior under retrieval, reranking, model-provider, timeout, overload, partial-output, cache, or dependency failures. Covers bounded retry, fallback graphs, budgets, circuit breaking, degraded modes, and abstention. Do not use to implement Provider transports, retrieval algorithms, authorization policy, or evaluation thresholds.
---

# RAG Degradation and Fallback

## 目标

在依赖失败时优先保持授权、证据真实性和明确状态，而不是不惜代价返回文本。每个 retry、switch、skip、cache、abstain 或 fail 决策都必须有前置条件、预算和停止点。

## 开始前

1. 画出在线请求各阶段、依赖、可选阶段、总 deadline、成本预算和用户可见提交点。
2. 列出结构化 failure kind、retryability、幂等性、是否已输出内容、当前授权和证据充分性。
3. 明确不可降级的不变量：身份、tenant、ACL、发布版本、证据要求、敏感信息和合规限制。
4. 从 [共享职责矩阵](../_shared/responsibility-matrix.md) 获取 retrieval、security、Provider 和 evaluation 边界。
5. 先写依赖故障、预算耗尽、fallback loop、partial stream、旧 cache 和绕权测试。

## 选择性读取

- 设计 failure taxonomy、预算、fallback graph、cache 或 abstain 时，读 [degradation-policy.md](references/degradation-policy.md)。
- 编写故障注入、属性测试或上线门禁时，读 [verification-playbook.md](references/verification-playbook.md)。
- 决策输入输出使用共享 `RequestContext`、`RetrievalTrace`、`ProviderResult` 与 `FallbackDecision` Schema。

## 核心工作流

1. **分类故障**：确认失败阶段、kind、retryability、幂等性、影响范围和是否已提交输出。
2. **计算剩余预算**：从原始绝对 deadline、attempt、cost 和 token 使用量计算，不能重置。
3. **检查硬约束**：authorization、tenant、版本和证据要求不满足时直接 abstain/fail。
4. **匹配有向边**：仅选择 fallback graph 中前置条件全部满足的一条边。
5. **执行一次动作**：retry、switch provider/retrieval、skip optional stage、validated cache、abstain 或 fail。
6. **重新评估**：消费新结果，增加 attempt 与成本；达到上限或无合法边时停止。
7. **标记模式**：normal、degraded、partial、abstained 或 failed 对用户与 telemetry 都可见。
8. **验证政策**：用故障矩阵、属性测试和生产指标证明无循环、无绕权和无静默成功。

## 必须保持

- fallback graph 有限且无环；每个动作受 max attempts、deadline、成本和并发限制。
- 下游只继承剩余 deadline，retry 或切换依赖不能重新获得完整预算。
- 只有 retryable、可安全重放且未提交 stream 的操作可以透明 retry。
- fallback 使用相同或更严格的 `AuthorizationContext`；授权失败没有“可用性优先”路径。
- vector 到 keyword、Reranker 到 base rank、主 Provider 到备用 Provider 都需要兼容性与证据充分性检查。
- cache 必须匹配 tenant、授权 scope/policy version、index/version、query、配置、model/prompt version 与有效期，并在读取后复核资源。
- 已经输出部分 stream 时明确标记 partial/error，不从另一 Provider 拼接成正常完成。
- 证据不足、冲突无法解决或只有模型常识时必须澄清、abstain 或 fail。
- 每个决定记录 failed stage、failure kind、action、attempt、remaining budget、授权保持、证据状态和原因。

## 禁止行为

- 不在授权、认证或 tenant context 失败时退回无 ACL 检索或纯模型回答。
- 不无限 retry、Provider A/B 循环、层层重置 timeout 或忽略成本上限。
- 不把单通道、无 Reranker、旧 cache 或 partial output 冒充正常模式。
- 不使用未验证的 semantic answer cache 作为企业事实证据。
- 不在 policy 中解析 Provider SDK 私有异常；先由 adapter 归一化。
- 不借降级改写底层检索、Provider adapter、ACL 或评测实现。

## 完成标准

交付 failure taxonomy、预算、无环 fallback graph、决策表、用户模式、telemetry 和停止条件。运行每条 graph edge 的正反测试、预算属性测试、跨租户与证据不足测试，并报告降级率、成功率、abstain 率、额外延迟/成本和剩余风险。
