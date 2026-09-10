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
