# 开发路线与工作流

## 当前阶段（2026-10-07）

- P0 本地代码交付和本机真实闭环已完成；生产最终验收仍受托管资源、正式身份、Linux
  prefork 和容量/RPO/RTO 条件限制。Linux 故障演练及 50 QPS/10 万文档压测按用户要求跳过。
- P1 已开始，检索五模式消融契约和脱敏 RAG 质量门禁机制已完成离线验证。
  安全/可靠性 JUnit 回归门禁也已完成：12/12 场景、17/17 必需测试通过。
  评测集审计也已完成：40 条 draft 结构和覆盖通过，但尚未 frozen/人工确认，
  因此不表示质量目标达标。另已补充 Fake 应用层性能基线、运行态 p50/p95 和
  真实 LM Studio Excerpt benchmark；完整模型链路性能与生产容量仍待实验。
- 下一阶段：取得授权 frozen golden 与人工审阅标注，校准质量门槛；归档 ACL/撤权与
  Celery 重投递/死信/replay 回归证据；接入真实 Weaviate ranking 后执行消融。
  真实生产容量仍不是当前承诺。

## 当前进度

**2026-10-03 更新：** 真实上传到引用、DB/对象/双路索引核验、跨tenant与撤权、
8类依赖故障注入已通过，前后端由根 `start.sh` 管理。
当前Embedding为 `bge-m3:latest`/1024维，历史qwen实验不覆盖当前配置。
全量离线303 passed/12 skipped，真实PostgreSQL16 passed。
追加本机免Token TXT/308279字节PDF真实UI上传、双路/模型/引用通过；
最新证据 `artifacts/experiments/2026-10-03_no-token-text-pdf/`，不代表OCR/公网模式。
多文件与无项目层面上传数量/大小上限已真实验收：3个独立文档（含
12583979字节PDF）同时选择、发布、ready、检索和引用通过；证据见
`artifacts/experiments/2026-10-03_multi-upload/`。不代表物理无限或生产容量结论。
证据见 `artifacts/experiments/2026-10-03_runtime-acceptance/`。
以下旧数字为历史阶段记录，不能代替本轮结果。

- 开工前置文档：已完成。
- 项目级 RAG Skill suite：已完成 5 个 Skill、共享契约、40 个压力场景及 CI 验证，`.agents/skills/` 为唯一权威目录。
- 评测集草案：已生成 40 条，待抽样确认。
- 第 1 轮代码骨架：已完成第一批基础件。
- Phase 3 PostgreSQL 基础切片：数据模型、显式事务、migration、发布约束、连接池、事务级超时、Compose migration job、CI PostgreSQL job 和 schema readiness 已完成并取得真实验证证据。
- 历史基线已验证：离线 `126 passed, 11 skipped`，有效开发 DSN `137 passed`；BM25F 中文对照阶段曾记录离线 `200 passed, 11 skipped`；当前 Task 8 最新离线代码级回归为 `261 passed, 12 skipped`。本轮 PostgreSQL 集成因未注入 DSN 明确跳过；该结果是离线回归，不是真实依赖 smoke 证据。
- 已完成：配置契约、Provider 协议/错误分类、Fake LLM、有界本地 LLM 调度器、本地对象存储适配器、领域实体、最小 API，以及 BGE Reranker 的统一异步 Adapter。
- 已完成：LM Studio OpenAI-compatible Provider、JSON/SSE 摘录问答、取消/超时清理、评测与 benchmark、精简工作台，以及 Markdown/HTML/受控文本 PDF 的真实摄取基础。
- 当前已取得真实 Weaviate `1.27.10` 证据：Dense collection/batch/integrity/nearVector/delete，以及 BM25F schema/weighted query/tenant-version filter/cleanup 均通过；默认读取路径仍是 PostgreSQL。
- 固定小数据集 BM25F/RRF 质量实验已完成：Dense、BM25F、RRF `k=20/60/100` 的 Recall@K、MRR、nDCG、独有命中和 p50/p95 延迟均已记录；RRF 在 top 3 补足双相关查询，但三个 `k` 无法区分。
- BM25F 中文 tokenizer/预处理对照已完成：baseline `word + identity` @3 为 `0.625/0.625/0.625`，`trigram + identity` 为 `0.75/0.75/0.75`，`word + cjk_bigram` 为 `1.0/1.0/1.0`；这些结果仅来自 10 个合成 chunk 和 8 条查询。
- 当前已完成：方案 C 本地 P0 产品代码闭环，包括同步上传、双路 generation 发布、开发身份 ACL、Dense+BM25F+RRF+BGE 查询、Parent/引用复核、状态/ACL 管理、产品 readiness 和请求体预算。
- 当前待处理：人工确认数据集、中文profile泛化、生产容量、备份恢复和故障转移；
  真实本地上传到引用smoke已于2026-10-03完成，仍不外推生产结论。
- 当前非目标：Keycloak/OIDC、Celery/RabbitMQ、Redis、Git 同步、OCR、Minikube、在线 Provider fallback 和生产容量结论继续后置；不得把离线/SQLite 或合成小样本结果写成真实生产证据。

## 目标版本

**V1 面试交付版：P0 完整 + P1 关键证据 + P2 可演进。** 预计 11～14 轮主开发，必要时增加 1～2 轮收敛。每轮必须有可运行增量、测试结果和状态更新。

## 轮次规划

| 轮次 | 交付重点 | 验证 |
|---:|---|---|
| 1 | 项目事实、规范、骨架 | 配置、迁移、最小 API、ADR/实验模板 |
| 2 | PostgreSQL 基础设施与数据模型 | migration、连接池、readiness、事务/约束集成证据；其他基础设施健康检查后置 |
| 3 | 模型契约与网关 | Ollama Embedding、进程内 BGE Reranker、LM Studio、本地/在线协议、超时和流式 |
| 4 | Markdown/HTML/受控 PDF 摄取 | 解析、清洗、失败隔离、幂等、重试分类 |
| 5 | Parent/Child 与索引 | Dense、BM25F、版本发布、可重建性 |
| 6 | 双路检索与排序 | RRF、Reranker、Parent 回溯、检索消融 |
| 7 | Linear 问答与 SSE | Prompt、引用、无答案、断连、取消、超时 |
| 8 | 开发身份、租户、ACL | 召回前过滤、返回后复核、引用安全；Keycloak/OIDC 后置 |
| 9 | 可靠性与恢复 | Outbox/等价机制、幂等、重放、Checkpoint、故障注入 |
| 10 | 有界 Agentic | Router、Grade、Rewrite、一次重检索、deadline、降级 |
| 11 | 评测与质量门禁 | 检索、生成、引用、安全、回归和消融 |
| 12 | 观测与压测 | JSON、Prometheus、首 token、P95、本地/替身/在线分层 |
| 13 | Compose、Minikube、故障演练 | 镜像、探针、资源限制、持久化、重启恢复 |
| 14 | 最终审计与面试材料 | 架构图、ADR、实验报告、深挖问答、未完成项 |

## 每轮 SOP

1. 读取 `PROJECT_CONTEXT.md` 和相关专题文档。
2. 明确本轮单一目标、非目标和验收条件。
3. 先写/更新测试或契约，再实现最小垂直切片。
4. 运行单测、集成测试、关键场景测试和回归测试。
5. 保存配置、日志、结果、失败案例和结论到 `artifacts/`。
6. 更新路线图、风险登记和入口状态。

## 模型使用策略

- 单测与状态机：Fake LLM。
- 高频集成和压测：LM Studio 本地 LLM + Ollama Embedding + 进程内 BGE Reranker。
- 在线质量：Grok/GPT 小规模抽样，不承担大规模回归或压测。
- `omlx`：仅在小模型 Router/Grade/Rewrite 的协议和并发基准通过后采用。

## 依赖阻塞处理

外部模型、镜像或服务不可用时，优先使用契约替身继续推进确定性测试；不得把替身结果写成真实模型质量结论。每个外部依赖恢复后补跑对应真实性验证。
