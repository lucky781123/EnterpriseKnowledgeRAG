# DeepEval CI 与生产评估

## CI 分层

| 层级 | 触发 | 内容 | 门禁 |
|---|---|---|---|
| deterministic | 每次 commit/PR | schema、dataset、引用、权限、trace contract | 任一失败阻断 |
| DeepEval smoke | PR | 少量高价值 cases、关键 metrics、固定 judge | 质量或 evaluator error 均不静默通过 |
| frozen regression | merge/release | 完整 frozen set、分组指标、normal/degraded | 绝对阈值与相对回归门禁 |
| scheduled/full | 定时 | 大数据集、消融、多个 judge 或重复运行 | 生成报告、告警和人工复核 |
| production sample | 异步采样 | trace/span metrics、漂移、安全和运维 | 趋势告警，不阻塞用户请求 |

DeepEval 官方当前推荐在 CI 中使用 `deepeval test run` 执行测试文件；脚本和实验可使用 `evaluate()` 收集结构化结果。具体 flags、cache 和并发配置先通过目标版本 `--help` 与官方文档验证，不复制未经确认的命令。

## 测试组织

- 一个测试聚焦一个可解释行为，按 query category、answerability 和 mode 参数化。
- materialization fixture 实际调用被测 RAG，填入 `actual_output` 与 `retrieval_context`。
- 慢或昂贵 cases 使用 marker/suite 分层，PR 不运行无界全集。
- evaluator timeout、quota、认证和 malformed response 返回独立 infrastructure status，不改写为 metric 通过或普通低分。
- 失败产物保存 metric score/reason、case ID、trace ID 和版本；敏感正文按策略脱敏。

## threshold 与门禁

同时考虑：

- hard invariants：unauthorized/stale evidence、伪造 citation、非法数据外发必须为零；
- absolute threshold：基于人工校准和可接受风险；
- relative regression：与已批准 baseline 比较，防止分数仍高但显著下降；
- slice threshold：对 forbidden、unanswerable、冲突、语言、query 类型和 degraded mode 单独设置；
- uncertainty：临界样本重复运行或人工复核，不把 judge 噪声当真实回归。

不要让平均值抵消严重安全失败。门禁配置本身版本化，并由明确 owner 审批。

## trace/span 归因

建议 trace 表示一次端到端请求，span 至少覆盖：

```text
authorization
query transformation
dense/sparse retrieval
fusion/rerank
context packing
generation
fallback decision
```

trace-level 评估回答端到端是否成功；span-level 评估定位 retriever、Reranker、generator 或 fallback 的贡献。将 `RetrievalTrace`、`ProviderResult` 和 `FallbackDecision` ID 关联到 evaluation observation，避免只保存最终文本。

官方参考：

- [Unit Testing in CI/CD](https://deepeval.com/docs/evaluation-unit-testing-in-ci-cd)
- [LLM Tracing](https://deepeval.com/docs/evaluation-llm-tracing)
- [End-to-End Evaluation](https://deepeval.com/docs/evaluation-end-to-end)

## 生产抽样

- 在线请求只记录轻量 trace；昂贵 judge 在异步 worker 中执行，不增加用户关键路径延迟。
- 按 query 类别、tenant tier、model、index、normal/degraded mode 分层采样，而不是只取随机平均流量。
- 对低频高风险事件全量捕获 metadata，但正文是否进入 evaluator 仍受隐私和授权控制。
- 外部 judge 前做数据最小化、脱敏、区域/保留审查；不允许的 tenant 使用本地 evaluator 或只运行确定性指标。
- 评估任务继承 tenant context，queue、cache、结果存储和 dashboard 均需隔离。

## 漂移与告警

监控 metric 分布、失败率、abstain、fallback、unauthorized block、latency、cost、judge infrastructure error，以及 query/document 分布变化。使用滑动窗口和最小样本数，避免单个非确定分数触发事故。

出现漂移时先确认 evaluator、judge、采样或数据分布是否变化，再归因到 retrieval、model、Prompt、policy 或 fallback。只有证据指向产品回归时才修改在线实现。

## 生产回流

把经过脱敏和人工确认的失败样本加入 development set；经过审查后才进入新版本 frozen set。保留原生产 trace ID、问题类别和失败原因，但不复制无授权数据。修复后先重现失败，再运行相关 slice 和完整回归。
