# 方案 B 真实检索 Smoke

本目录记录方案 B 的最小真实依赖闭环：固定 Markdown 摄取、真实 Ollama Embedding、PostgreSQL JSON 向量存储与应用层 cosine、tenant/version 过滤、Parent 上下文回溯，以及通过 FastAPI 调用本地 LM Studio 的摘录回答。

## 实验范围

- 向量后端固定为 `postgres_json_application_cosine`。
- PostgreSQL 只保存本次随机临时数据库中的业务事实和向量 JSON；不安装或启用 `pgvector`。
- 使用项目根目录 `.env` 中的开发 DSN，但 runner 只在进程内读取，报告不保存 DSN、密码、API key、Prompt、文档正文、citation quote 或完整模型回答。
- 真实生成只记录 HTTP、答案状态、引用校验、Provider 终态和耗时摘要。

## 复现

在项目根目录执行：

```bash
/Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/retrieval_smoke.py \
  --output artifacts/experiments/2026-09-16_retrieval-smoke/metrics.json
```

运行前需要本机开发依赖可用：PostgreSQL、Ollama 模型 `hardcore_aryabhata_598/qwen3-embedding:8b`，以及 LM Studio 模型 `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`。runner 会创建随机临时数据库和临时对象目录，并在结束时清理。

## 结果摘要

详见同目录的 `metrics.json` 和 `conclusion.md`。本次运行状态为 `completed`：

- PostgreSQL、Ollama 和 LM Studio 均可用。
- Ollama Embedding 维度为 `4096`，索引记录数为 `1`。
- 正确 tenant/version 命中 `1`，错误 tenant/version 命中均为 `0`。
- Top hit 使用 Parent 上下文，真实 FastAPI 返回 `200`，`context_source=retrieved`。
- Provider 完成，答案状态为 `answered`，引用身份校验为 `passed`。
- 临时数据库和对象目录均已清理。

## 判读边界

这是一条单机开发 smoke，不是检索质量评测、生产性能测试、高可用验证或容量承诺。它也不证明 `pgvector` 可用性、ACL/OIDC、BM25F/RRF、reranker、Weaviate 迁移或生产级发布流程。
