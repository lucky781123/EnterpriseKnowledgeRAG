# DeepEval RAG 数据集与指标

## 先验证目标版本

DeepEval API 会演进。实现前先检查 lock file 和安装版本，再打开对应官方文档，并运行目标命令的 `--help`。本参考描述稳定概念；constructor 参数、CLI flag 和 async/config 细节以目标版本为准。

## Golden 与 LLMTestCase

- `Golden` 是运行前模板：稳定 case ID、用户输入、可选 expected output、理想 context、answerability、权限场景、类别和 metadata。
- `LLMTestCase` 是被测 RAG 实际运行后的记录：input、actual output、实际 `retrieval_context`、可选 expected output/ideal context，以及运行 metadata。
- `EvaluationDataset` 管理和版本化 Golden 集合；测试执行器负责逐个运行应用并 materialize test cases。

不要把 Golden 中的理想 context 复制成实际 retrieval context。这样会让 retriever 指标永远看起来正确，并切断对线上 pipeline 的诊断。

## 推荐数据字段

| 字段 | 作用 |
|---|---|
| stable case ID | 跨版本比较、去重和失败定位 |
| input | 真实用户问题或多轮上下文 |
| answerability | `answerable`、`unanswerable`、`conflict`、`forbidden` |
| expected evidence | 文档、版本、章节、事实或允许的证据集合 |
| expected output/boundary | 可选标准答案或必须/禁止包含的结论 |
| tenant/roles/policy | 权限与安全场景，生产报告中需脱敏 |
| category/tags | query 类型、语言、难度、产品路径 |
| split | smoke、development、frozen、production sample |
| provenance | 作者、来源、审核者、生成/人工状态和变更原因 |

合成 Golden 只能作为候选。进入 frozen 前抽样验证问题自然度、证据真实性、权限边界、冲突语义和不可回答性。

## metric 与字段映射

下表是常见映射，调用前仍需核对安装版本的必需参数：

| 维度 | 常见 DeepEval metric | 典型字段 | 回答的问题 |
|---|---|---|---|
| retriever relevance | Contextual Relevancy | input、retrieval_context | 检索内容是否与问题相关 |
| retriever ordering | Contextual Precision | input、expected output/证据、retrieval_context | 相关证据是否排在无关证据前 |
| retriever coverage | Contextual Recall | expected output/证据、retrieval_context | 所需事实是否被检索覆盖 |
| generator grounding | Faithfulness | actual output、retrieval_context | 输出主张是否得到实际证据支持 |
| generator relevance | Answer Relevancy | input、actual output | 回答是否回应问题 |
| end-to-end correctness | GEval 或任务 metric | input、actual output、expected output、criteria | 是否满足领域答案标准 |

补充确定性指标：期望 document/chunk hit、Recall@k、MRR/nDCG、citation identity、unauthorized hit、stale version hit、abstain correctness、latency、token、cost、fallback mode。安全与身份指标不交给 LLM judge 代替。

## answerability 评估

- `answerable`：证据命中、grounding、答案相关性和任务正确性。
- `unanswerable`：应 abstain 或澄清；流畅但无证据的回答是失败。
- `conflict`：必须识别冲突、保留版本/来源，不能合并成确定事实。
- `forbidden`：任何受保护内容、引用或存在性泄漏都是 hard failure；不因措辞礼貌而通过。

按类别分别报告。总体平均值可能掩盖 forbidden 泄漏或少数高风险 query 的系统性失败。

## 数据集治理

- smoke：小、快、覆盖关键契约，PR 可运行。
- development：允许频繁分析和调参，不能代表无偏泛化。
- frozen：人工确认并版本锁定，用于回归与发布；修改需评审和变更记录。
- production sample：按隐私和授权策略采样，不能自动并入 Golden。

防止近重复问题跨 split；记录文档、索引、Prompt、retrieval config、model 和 policy version，避免数据污染或版本错配。

## judge 校准

1. 由领域人员标注一组 good/bad/borderline 样本和原因。
2. 固定 judge model、criteria、DeepEval 版本和可控随机性，多次运行估计波动。
3. 比较 judge 与人工标签的误报、漏报和分歧类别，而不只看相关系数。
4. 根据业务风险选择 threshold；高风险安全规则使用确定性 hard gate。
5. 临界分数、judge 理由异常或新分布进入人工复核队列。
6. judge/model/criteria 变更时重新校准，不能直接与旧分数拼接趋势。

## 可复现结果

每次 run 记录 dataset version、case IDs、应用 commit、配置版本、索引、Embedding、Reranker、generator、fallback mode、DeepEval、metric 参数、judge、并发/cache 设置、时间、成本和失败类型。Evaluator infrastructure error 与 product metric failure 必须分开。

## 官方参考

- [Evaluation Datasets](https://deepeval.com/docs/evaluation-datasets)
- [LLM Test Cases](https://deepeval.com/docs/evaluation-test-cases)
- [RAG Evaluation](https://deepeval.com/docs/getting-started-rag)
- [Metrics Introduction](https://deepeval.com/docs/metrics-introduction)
