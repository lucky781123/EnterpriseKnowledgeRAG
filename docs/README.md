# 文档导航

## 项目入口

- [项目上下文](../PROJECT_CONTEXT.md)：目标、边界和当前事实。
- [开发路线](./roadmap.md)：阶段、里程碑和交付顺序。
- [技术栈](./technology-stack.md)：组件候选、运行约束和取舍。
- [架构](./architecture.md)：数据流、权限闭环和非目标。
- [验收矩阵](./acceptance-matrix.md)：P0/P1/P2 验收标准与状态语义。
- [2026-10-03真实运行验收](../artifacts/experiments/2026-10-03_runtime-acceptance/README.md)：启动、HTTP/DB/索引/降级/浏览器证据与复现。
- [免Token TXT/大PDF追加验收](../artifacts/experiments/2026-10-03_no-token-text-pdf/README.md)：最新本机免Token配置、真实上传/回答/持久化和故障日志。
- [多文件与无上传上限验收](../artifacts/experiments/2026-10-03_multi-upload/README.md)：3个独立文档、12 MiB PDF、真实UI和持久化证据。
- [风险登记](./risk-register.md)：已知风险、触发条件和缓解方案。

## 开发与运维

- [基础设施渐进改造](./architecture/infrastructure-evolution.md)：Redis/Celery/MinIO、备份恢复、DeepSeek 的已实现边界、目标架构、迁移与回滚。
- [环境预检](./preflight-checklist.md)：本地模型和基础设施检查项。
- [本地 Reranker](./reranker-local.md)：BGE Reranker 安装与 smoke test。
- [ADR 规范](./adr/README.md)：重要技术决策的记录格式。

## 评测与面试材料

- [评测规范](./evaluation/evaluation-plan.md)
- [评测集规范](./evaluation/dataset-spec.md)
- [评测集草案](./evaluation/dataset-draft.jsonl)
- [面试证据与问题](./interview/evidence-and-questions.md)
- [高级 RAG 工程面试问答集](./interview/advanced-rag-engineering-qa.md)：基于当前源码、配置和验收证据的问题驱动深度问答。
- [初学者可读 HTML 版](./interview/advanced-rag-engineering-qa.html)：73 题逐步解释、每步核心代码、离线时序图与简版口述；正文与 Markdown 同源。

评测集中的 `expected_evidence` 目前引用一批尚未全部纳入仓库的企业文档样本，因此当前只能作为结构草案，不能视为可直接复现的完整评测基线。
