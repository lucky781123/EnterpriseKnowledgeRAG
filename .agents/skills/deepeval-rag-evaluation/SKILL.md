---
name: deepeval-rag-evaluation
description: Use when designing, implementing, debugging, or governing DeepEval-based RAG datasets, test cases, retriever and generator metrics, LLM-as-a-judge calibration, CI gates, tracing, production sampling, regression analysis, or drift monitoring. Do not use to implement the online retrieval, authorization, Provider, or fallback runtime itself.
---

# DeepEval RAG Evaluation

## 目标

建立可复现、可归因、成本有界的 RAG 评估体系。将 dataset 模板、运行时 test case、retriever、generator、end-to-end、安全和运维指标分开，使 CI 失败能定位原因，生产漂移能连接到具体 trace/span 与版本。

## 开始前

1. 检查项目实际安装的 DeepEval 版本、lock file、官方文档和可用 CLI；不凭记忆使用 API 或 flag。
2. 明确评估目的、用户/查询分布、风险、数据隐私、成本、运行时间和谁负责人工确认。
3. 盘点可观测字段：input、actual output、expected output、ideal context、retrieval context、trace、mode、tenant/ACL outcome 和版本。
4. 从 [共享职责矩阵](../_shared/responsibility-matrix.md) 获取其他 Skill 的 trace/result/decision 边界。
5. 先写一个能暴露目标回归的最小 eval case，确认旧系统或错误 fixture 会失败。

## 选择性读取

- 设计 Golden、`LLMTestCase`、数据集 split、metric 或 judge 校准时，读 [dataset-and-metrics.md](references/dataset-and-metrics.md)。
- 设计 PR gate、定时评估、trace/span 在线评估、生产抽样或漂移告警时，读 [ci-and-production.md](references/ci-and-production.md)。
- 结构化结果使用共享 `RetrievalTrace`、`ProviderResult`、`FallbackDecision` 与 `EvaluationObservation` Schema。

## 核心工作流

1. **定义问题与风险**：把“RAG 质量”拆成 retriever、generator、end-to-end、安全和 operations 维度。
2. **建立 Golden**：记录稳定 ID、输入、期望边界、理想证据、answerability、权限场景、类别和 split。
3. **运行系统形成 test case**：捕获实际 `actual_output`、`retrieval_context`、trace、fallback mode 与版本，不能用理想值替代实际值。
4. **匹配 metric 字段**：按已安装版本核对每个 metric 的必需字段、judge model、threshold 和 strict/async 行为。
5. **校准 judge 与 threshold**：用人工标注样本、重复运行、误报/漏报成本和 baseline 分布确定，不照搬默认值。
6. **分层运行**：本地/PR smoke、merge/frozen、scheduled full、production sample 各有独立成本和阻断规则。
7. **定位失败**：连接 case、metric、reason、retrieval trace、Provider result、fallback decision、代码与配置版本。
8. **监控漂移**：按 query 类别、tenant tier、normal/degraded mode、模型和索引版本看趋势，并安排人工复核。

## 必须保持

- `Golden` 是运行前模板，`LLMTestCase` 是运行系统后带实际输出的评估单元。
- `context` 表示理想或 ground-truth context；`retrieval_context` 表示实际检索内容，两者不能互换。
- retriever 指标、generator 指标和 end-to-end 指标分别报告；单个总分不能替代诊断维度。
- answerable、unanswerable、conflict 和 forbidden 场景都进入数据集，安全失败使用确定性 hard gate。
- smoke、development、frozen 与 production sample 分开版本；不在 frozen set 上反复调参。
- metric 必需字段和 DeepEval API 在目标版本中验证；字段缺失是评估错误，不是零分或通过。
- judge model、prompt/criteria、DeepEval 版本、随机性设置、并发和 cache/config 被记录并尽可能固定。
- threshold 来自业务风险、人工校准、baseline 与置信区间；临界样本重复评估或进入人工复核。
- normal 与 degraded/fallback mode 分开评估，不能用 fallback 成功率掩盖质量下降。
- 生产数据外发给 judge 前取得明确授权并做最小化、脱敏、tenant 隔离和保留控制。

## 禁止行为

- 不用最终答案看起来合理来推断 retrieval 正确。
- 不把 evaluator timeout、认证失败或 quota 问题当作产品 metric 通过。
- 不把单次非确定 judge 分数当成绝对事实，也不只优化平均分。
- 不在 PR 中运行无界全量评测或让在线评估拖慢用户请求。
- 不把敏感生产文档默认发送给外部 evaluator。
- 不让 evaluation Skill 修改在线 ACL、fallback、Provider 或检索实现；它提供证据和门禁。

## 完成标准

交付版本化 dataset、materialization 流程、metric-to-field 映射、judge calibration、threshold rationale、CI 分层、trace/span 方案、生产抽样与漂移策略。运行确定性数据校验、DeepEval smoke/frozen 测试和 evaluator failure 测试，并报告成本、耗时、波动、失败归因和未覆盖分布。
