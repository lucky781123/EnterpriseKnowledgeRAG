---
name: retrieval-pipeline-engineering
description: Use when designing, implementing, tuning, or debugging RAG ingestion, chunking, indexing, retrieval, fusion, reranking, context packing, or evidence lineage. Do not use for access-control policy, model-provider API plumbing, fallback policy, or evaluation governance.
---

# Retrieval Pipeline Engineering

## 目标

构建可重复、可发布、可追踪的检索流水线。任何优化都必须能定位到具体阶段，并通过检索结果、trace 或测试验证，而不是只凭最终回答观感。

## 开始前

1. 检查已有文档模型、索引、检索器、Reranker、测试和评测数据，不猜测技术栈。
2. 明确输入文档类型、查询类型、数据规模、延迟预算、token 预算和发布语义。
3. 写出当前数据流、每阶段契约、失败路径和成功指标。
4. 从 [共享职责矩阵](../_shared/responsibility-matrix.md) 获取跨 Skill 边界。
5. 涉及实现或修复时，先写能暴露目标行为的失败测试。

## 选择性读取

- 设计或修改摄取、索引、召回、融合、重排、Parent 回溯或 context packing 时，读 [pipeline-design.md](references/pipeline-design.md)。
- 编写测试、诊断质量或准备验收时，读 [verification-playbook.md](references/verification-playbook.md)。
- 需要结构化输出时，使用共享 `RequestContext`、`AuthorizationContext` 和 `RetrievalTrace` JSON Schema。

## 核心工作流

1. **固定事实**：记录数据源、版本事实源、索引实现、Embedding/Reranker、授权过滤接口和当前指标。
2. **定义契约**：为 Document、Version、Chunk、Candidate、Evidence 和 `RetrievalTrace` 明确稳定 ID 与 lineage。
3. **建立摄取闭环**：验证 → 解析 → 规范化 → 切块 → 派生数据 → 完整性检查 → 原子发布。
4. **建立查询闭环**：规范化 → 授权过滤 → 单路/多路召回 → 融合 → 重排 → Parent 扩展 → 去重 → context packing。
5. **分类失败**：指出失败阶段、影响范围和可否重试；把结构化故障交给 degradation Skill，不在这里发明业务 fallback。
6. **验证贡献**：分别衡量召回、排序、上下文和端到端结果；一次只改变一个主要变量。

## 必须保持

- 同一内容和切块策略生成稳定、可重放的 ID；重复摄取不得产生重复可见数据。
- 新版本只有在所有必需派生物通过完整性检查后才能发布；查询不得看到半成品版本。
- 每个证据保留 tenant、document、version、chunk、parent 和 source lineage。
- dense、sparse、metadata 等所有召回通道使用同一授权约束；融合和重排不能重新引入未授权候选。
- Parent 扩展、引用详情和缓存回源重新执行授权与版本检查。
- top-k、融合、Reranker 和 token budget 的配置有评测依据，不能仅凭直觉固定。
- context packing 显式处理去重、冲突、顺序、token 预算和截断原因。
- 每个阶段记录输入数、输出数、耗时、状态和失败类型，且不记录敏感原文或凭据。

## 禁止行为

- 不把索引当作权限或版本的唯一事实源。
- 不在文档已撤权或新版本未发布时继续返回旧缓存证据。
- 不混用不同版本的冲突片段拼成一个看似确定的答案。
- 不用无上限 top-k、全量扫描或无限查询改写掩盖召回问题。
- 不因 Reranker 失败而静默声称正常模式。
- 不把检索调优扩大为 ACL、Provider 或评测框架重构。

## 完成标准

报告修改过的阶段和契约、测试的 RED/GREEN 证据、检索指标变化、延迟/成本变化、失败模式以及仍未验证的假设。运行 Skill suite validator，并确保压力场景覆盖 happy path、边界、故障和安全类别。
