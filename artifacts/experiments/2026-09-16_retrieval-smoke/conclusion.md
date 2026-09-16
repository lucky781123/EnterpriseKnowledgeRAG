# 实验结论

## 已确认

- 方案 B 的真实最小链路已跑通：固定 Markdown 摄取 -> Ollama Embedding -> PostgreSQL JSON 向量写入 -> 应用层 cosine 检索 -> Parent 回溯 -> FastAPI 检索问答 -> LM Studio 摘录回答。
- 本次真实运行使用的 Embedding 模型为 `hardcore_aryabhata_598/qwen3-embedding:8b`，输出维度为 `4096`；本地生成模型为 `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`。
- tenant/version SQL 过滤生效：正确作用域命中 `1`，错误 tenant 和错误 version 均为 `0`。
- 检索结果能够回溯 Parent 上下文；真实 API 返回 HTTP `200`、`context_source=retrieved`、`answer_status=answered`、`citation_validation=passed`，Provider 终态为 `success` 且 `finish_reason=stop`。
- PostgreSQL 的 `vector` 扩展只做了只读 available/installed 检查；本次环境中二者均为 `false`，因此仍使用 JSON + 应用层 cosine 路径。
- runner 最终清理了随机临时数据库和对象目录。

## 已实现

- `scripts/retrieval_smoke.py` 提供可复现的真实依赖 smoke，并对报告执行递归脱敏。
- `tests/test_retrieval_smoke.py` 覆盖临时数据库 URL 密码保留、扩展状态归一化和报告脱敏边界。
- `POST /api/v1/retrieval/query` 已复用现有 embedding、PostgreSQL repository、Parent 去重和 F2-G 摘录回答契约。

## 待实验

- 更大规模文档集、多个 tenant/version、删除与重建、并发和故障恢复。
- Recall@K、MRR/nDCG、人工事实性与引用质量、成本、GPU 显存及长期延迟分布。
- Dense 向量迁移到 Weaviate 后的双写、回填、切换、回滚和可重建发布。

## 非目标

- 本实验不安装或启用 `pgvector`，不接入 Weaviate、BM25F/RRF、reranker 或 ACL/OIDC。
- 不把单机 smoke 的成功、耗时或单条命中外推为生产性能、容量、高可用、SLO 或企业语义质量。
- 不保存完整模型回答、Prompt、文档正文、citation quote、DSN、密码或 API key。
