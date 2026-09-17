# 结论

## 已确认

- 固定数据集包含 10 个合成技术 chunk 和 8 条查询；结论仅适用于该数据集。
- RRF k=20/60/100 的结果完全一致，当前样本不能支持生产参数选择。

## 已验证

- Dense @3: Recall=0.9375，MRR=1.0000，nDCG=0.9516。
- BM25F @3: Recall=0.6250，MRR=0.6250，nDCG=0.6250。
- RRF @3: Recall=1.0000，MRR=1.0000，nDCG=1.0000。
- Dense 独有命中 3 条语义查询，BM25F 独有命中 0 条；BM25F 在双相关发布查询中补足了 Dense 排名第 4 的 trace 证据，使 RRF 在 top 3 内召回两个相关 chunk。
- 查询延迟 p50/p95：Dense 5.527/6.298 ms，BM25F 3.262/3.616 ms；RRF 融合耗时低于 0.1 ms。

## 待实验

- 扩展经人工确认的企业查询与文档分布，并在 development/frozen split 上复测。
- 调整中文分词与查询预处理，复测 BM25F 对语义改写无结果的问题。
- 当前结果不能确定生产默认 RRF k、BM25F 权重、容量或 SLO。

## 非目标

- 本实验未接入默认 API、Sparse Generation、Reranker 或 ACL/OIDC。
