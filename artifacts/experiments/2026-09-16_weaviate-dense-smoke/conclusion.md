# 结论

## 已确认

- PostgreSQL 事实模型、Dense generation 状态、active/previous pointer、回填选择、retire 和 cleanup 编排已实现。
- Weaviate transport 已有 HTTPX contract，包括 collection schema、批量 upsert、generation/version 过滤、查询、删除和固定错误边界。
- 默认 API 读取路径仍是 PostgreSQL JSON + 应用层 cosine；Weaviate 只通过显式依赖注入启用。

## 已验证

- 离线 transport、indexer、generation、回填和 API 注入测试可复现。
- 本次 smoke runner 能在未启用 Weaviate 时输出脱敏 `skipped` 报告。

## 待实验

- 真实 Weaviate collection 创建、批量写入、完整性检查、查询、删除以及 PostgreSQL 发布/回滚联调。
- 真实服务版本差异、批量响应语义、对象分页和网络故障下的重试观察。

## 非目标

- 不据此宣称生产容量、召回质量、SLO、跨系统原子事务或 ACL/OIDC 已完成。
- 本轮不实现 BM25F、RRF、reranker、默认读路径切换或生产部署。
