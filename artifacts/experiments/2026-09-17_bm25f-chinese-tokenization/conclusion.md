# 结论

## 已确认

- 三组 profile 复用同一批 Dense 结果、字段权重、candidate K、RRF 公式和固定数据集。
- `trigram + identity` 只改变 Weaviate tokenizer；`word + cjk_bigram` 只改变索引与查询共同使用的预处理。
- Weaviate `1.27.10` 当前实例接受 `trigram`，实验结束后四个隔离 collection 均已删除。

## 已验证

- Dense @3 Recall/MRR/nDCG=0.9375/1.0000/0.9516。
- `word + identity` BM25F @3=0.6250/0.6250/0.6250，p50/p95=3.428/4.065 ms。
- `trigram + identity` BM25F @3=0.7500/0.7500/0.7500，RRF k=60 @3 nDCG=0.9900。
- `word + cjk_bigram` BM25F @3=1.0000/1.0000/1.0000，p50/p95=3.459/3.724 ms。
- `trigram` 仍未召回幂等和撤权两条语义改写，并给技术查询带来更多低分候选；`cjk_bigram` 在当前 8 条查询上全部命中。

## 待实验

- `cjk_bigram` 的满分只来自 10 个合成 chunk 和 8 条同域查询，可能过拟合措辞与字符重叠。
- 在人工确认的 development/frozen split 上扩大中文同义改写、短查询、错误码、路径和干扰文档后，再决定是否采用该 profile。
- 评估索引体积、长文档噪声、低分候选阈值和更大 candidate K；当前延迟不代表容量或 SLO。

## 非目标

- 本实验未实现 Sparse Generation、默认 API 编排、Reranker、ACL/OIDC 或生产参数冻结。
