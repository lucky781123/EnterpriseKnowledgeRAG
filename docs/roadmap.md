# 开发路线与工作流

## 当前进度

- 开工前置文档：已完成。
- 项目级 RAG Skill suite：已完成 5 个 Skill、共享契约、40 个压力场景及 CI 验证，`.agents/skills/` 为唯一权威目录。
- 评测集草案：已生成 40 条，待抽样确认。
- 第 1 轮代码骨架：已完成第一批基础件。
- Phase 3 PostgreSQL 基础切片：数据模型、显式事务、migration、发布约束已完成；连接池、事务级超时、Compose migration job、CI PostgreSQL job 和 schema readiness 正在收口。
- 已通过：Python 编译检查、23 个测试、评测集加载、FastAPI 应用导入、Skill suite validator 和 5 个 Skill 官方结构验证。
- 已完成：配置契约、Provider 协议/错误分类、Fake LLM、有界本地 LLM 调度器、本地对象存储适配器、领域实体、最小 API，以及 BGE Reranker 的统一异步 Adapter。
- 已知待处理：真实 Provider、摄取、检索、请求级权限闭环和问答链尚未实现；PostgreSQL 生产容量、备份恢复和故障转移也未验证。
- 当前下一步：完成 PostgreSQL 连接可靠性与部署集成的真实 CI/本地验证，再进入摄取基础；不把单机 Compose 结果写成生产容量结论。

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
| 8 | Keycloak、租户、ACL | 召回前过滤、返回后复核、缓存和引用安全 |
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
