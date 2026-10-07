# 评测与实验规范

## 1. 数据集分层

- `smoke`：约 100 条，快速回归。
- `development`：约 200 条，用于调参和消融。
- `frozen`：约 100 条，只在候选版本验收时使用。
- `security`：越权、撤权、Prompt Injection、版本冲突和无答案专项。

首期可以使用公开研发文档与合成企业数据，但必须标记数据来源；人工抽样校准相关性、证据支持和安全标签，不把合成标签当成真实企业标注。

## 2. 检索实验

固定数据、问题、权限、上下文预算和 Reranker 版本，只改变一个主要变量。至少比较：

```text
Dense
BM25F
Dense + BM25F
Dense + BM25F + RRF
Dense + BM25F + RRF + Reranker
```

记录 Recall@K、MRR/nDCG、证据进入候选比例、Parent 覆盖率、每路独有命中、延迟和资源消耗。Reranker 不能弥补候选集中不存在的证据。

当前 P1 已提供统一的脱敏消融入口：

```bash
PYTHONPATH=src conda run -n enterprise-rag \
  python -m scripts.run_retrieval_ablation \
  --dataset docs/evaluation/retrieval-ablation-contract-fixture.json \
  --output /private/tmp/retrieval-ablation-report.json \
  --k 5 --rrf-k 60
```

该 fixture 只验证输入契约、指标计算和报告格式，`dataset_kind` 已明确标记为
`deterministic-contract-fixture`，不能作为真实 Weaviate、模型质量或生产容量结论。
真实实验应把各模式的脱敏 ranking 和外部观测延迟写入同一契约后再运行。

离线 RAG 评测可通过 `scripts.evaluate_rag --thresholds <json>` 启用显式质量门禁。
配置仅接受 `min_retrieval_recall`、`min_citation_precision`、
`min_citation_support_rate`、`min_grounded_answer_rate`、
`min_abstention_accuracy`、`min_authorization_accuracy`，值域均为 `[0, 1]`；
未配置的指标不设默认阈值。门禁配置必须来自已审阅/校准的评测策略，不能把示例值
或合成 fixture 当成正式发布门槛。任一 evaluator 样本失败仍作为硬失败，不能被宏观
平均值抵消。门禁失败时 CLI 返回退出码 `2`，并在报告中写入观察值、门槛与脱敏失败类型。

P1 安全与可靠性回归门禁使用 pytest JUnit 结果和版本化清单：

```bash
conda run -n enterprise-rag python -m pytest -q \
  --junitxml=/private/tmp/p1-full.xml
PYTHONPATH=src conda run -n enterprise-rag \
  python -m scripts.run_p1_regression_gate \
  --junitxml /private/tmp/p1-full.xml \
  --manifest docs/evaluation/p1-regression-manifest.json \
  --output artifacts/experiments/2026-10-07_p1-regression-gate/verification.json
```

清单中的测试节点必须在同一次 JUnit 结果中出现且为 `passed`；`missing`、`failed`、
`error`、`skipped` 均阻断门禁。当前清单覆盖 tenant/ACL/撤权/引用出站、
Prompt Injection、外部调用 ACL 复核、Worker ACL 重载、重复投递、有限重试/死信、
人工 replay、outbox 重投递、lease fencing 和阶段幂等。该报告是本地测试证据，
不替代托管资源恢复、Linux prefork 故障演练或生产容量实验。

## 3. 生成实验

分开记录：

- Faithfulness；
- Citation Support；
- Answer Correctness；
- 无答案识别率；
- 错误拒答率；
- 版本冲突处理；
- 输出延迟和成本。

自动 Judge 必须固定模型、Prompt 和版本，并进行人工抽样复核。自动评测分数不是事实，也不替代检索层标注。

## 4. 模型运行分层

| 层次 | 用途 |
|---|---|
| Fake LLM | 确定性单测、状态机、SSE、故障和系统上限 |
| LM Studio 本地 LLM | 高频集成、真实本机链路、回归和本地压测 |
| Ollama | Embedding |
| 进程内 BGE Reranker | Reranking；通过 `SyncRerankerAdapter` 暴露异步契约 |
| 在线 Grok/GPT | 150～300 条代表性质量样本及低并发真实性能抽样 |
| `omlx` | 小模型运行时候选，需通过 Provider 和并发契约 |

## 5. 压测报告

必须分开报告：

1. `Fake LLM + 真检索`：系统基础容量。
2. `本地 LM Studio + Ollama Embedding + 进程内 BGE Reranker + 真 RAG`：Mac 本机端到端行为。
3. `在线 LLM + 真 RAG`：低并发供应商体验抽样。

每组记录队列延迟、TTFT、完成延迟、P50/P95、吞吐、错误率、取消率、输出长度、内存峰值和成本估算。不得把本地结果外推为云生产容量。

当前已补充运行态有界 histogram 的 nearest-rank `p50/p95`，并保留
`count/last_ms/max_ms` 兼容字段。Fake provider 应用层基线产物位于
`artifacts/experiments/2026-10-07_p1-local-performance/`，10/10 成功只证明
调度和结果校验路径；检索、真实模型、TTFT、GPU 资源和生产容量均为
`not_applicable` 或待真实本地链路实验。

当前目录也包含一次真实本机 LM Studio 小样本 Excerpt benchmark：
`local-benchmark-2.json`。JSON/SSE 各 2 次均成功；JSON 完成延迟 p50/p95 为
`1398/2388 ms`，SSE 完成延迟 p50/p95 为 `1011/1998 ms`，SSE TTFT p50/p95
为 `523/1523 ms`。该结果仅覆盖固定 evidence 的回答 Provider，不覆盖真实
Weaviate/Embedding/Reranker/PostgreSQL 链路，不能用于生产 P95、容量、成本或质量
门槛校准。

## 6. 实验产物命名

```text
artifacts/experiments/YYYY-MM-DD_<experiment-name>/
  README.md
  config.json
  metrics.json
  failures.jsonl
  conclusion.md
```

每份产物关联 commit、模型、Prompt、索引、数据集和运行时。
