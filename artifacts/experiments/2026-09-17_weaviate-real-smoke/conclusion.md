# 结论

## 已确认

- 项目 Compose 固定运行 Weaviate `1.27.10`，readiness 正常。
- Weaviate 1.27 的 GraphQL `limit` 通过 JSON variable 传入时会触发 `int64`/`int` 类型断言；使用已校验的整数参数内联可稳定查询。
- minimal 批量删除响应的实际删除数位于 `results.successful`。

## 已实现

- Dense transport 已兼容 UUID object ID、nested locator schema、动态 where 类型、内联 `limit` 和 1.27 删除计数。
- 新增最小 `WeaviateBM25FTransport` 和真实 smoke runner，复用 `SparseFieldProjector` 的四字段投影与固定字段权重。
- Compose 中 API 仍保持 `WEAVIATE_ENABLED=false`，默认 PostgreSQL 读取路径未切换。

## 已验证

- Dense：写入 2 条、完整性 2 条、nearVector 命中 1 条、cleanup 删除 2 条。
- BM25F：写入 1 条、正确 scope 命中 1 条、错误 tenant/version 均命中 0 条、cleanup 删除 1 条并删除隔离 collection。

## 待实验

- 中文分词、错误码、路径和 API symbol 的真实召回质量，以及字段权重对 Recall@K、MRR/nDCG 和延迟的影响。
- Dense/BM25F 双路编排、RRF、reranker、跨系统发布/回滚和规模化并发行为。

## 非目标

- 本实验不证明生产容量、高可用、SLO、企业语料质量、ACL/OIDC 闭环或默认读路径切换已经完成。
- 本轮不新增 Sparse Generation PostgreSQL 状态机，不实现 RRF 或自动发布。
