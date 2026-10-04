# P0/P1/P2 验收矩阵

## P0：可运行闭环

| 能力 | 验收条件 | 证据 | 当前状态（2026-10-03） |
|---|---|---|---|
| 文档摄取 | TXT/Markdown/HTML可上传；受控PDF成功/失败明确 | 集成测试、失败样本、产品上传API | 真实TXT和308279字节PDF免Token UI上传201/发布/回答通过；Markdown早期真实通过，HTML单测，Git未实现 |
| 解析切块 | Parent/Child、标题路径、版本和来源元数据正确 | 单测、样本快照 | 已验证 |
| 索引 | Embedding、BM25F、版本发布可完成 | generation 校验、查询测试、发布测试 | 真实两路 chunk IDs/count、1024维向量、hash、active pointer、四类 ready 产物均核验通过 |
| 检索 | Dense + BM25F + RRF + Reranker 可返回证据 | API 测试、引用样例 | 真实双路命中、RRF60、BGE、Parent/ACL通过；单依赖降级亦有真实依赖故障注入证据 |
| 问答 | Linear 能回答、无答案、引用和版本冲突 | 端到端测试 | 真实LM Studio answered/引用passed；无证据/撤权404、不伪造模型兜底 |
| 鉴权 | OIDC、租户、RBAC、文档 ACL 生效 | 越权测试 | 开发 HMAC Bearer token、tenant/role、文档 ACL 和撤权复核已验证；Keycloak/OIDC 后续演进 |
| 异步任务 | 失败、重试、幂等、人工重放路径可见 | 任务状态测试 | 摄取幂等/replay 已验证；队列 Worker 后续实现 |
| SSE | `stage/token/citation/error/done` 生命周期清晰 | 流式和断连测试 | F2-G 已验证 |
| 部署 | Compose 可启动；Minikube 基础部署可验证 | 部署日志和冒烟测试 | 本机Conda API+依赖容器前后端和一键脚本已验证；Minikube/产品镜像本轮未验证 |

本轮证据统一位于 `artifacts/experiments/2026-10-03_runtime-acceptance/`。
最新追加证据 `artifacts/experiments/2026-10-03_no-token-text-pdf/`，
多文件追加证据 `artifacts/experiments/2026-10-03_multi-upload/`：
真实Chrome一次选择TXT、Markdown和12583979字节PDF，3个独立文档发布/ready，
底层持久化、双路索引、RRF/BGE和引用问答均通过。
显式本机匿名固定演示身份，默认鉴权与ACL保留，不等同OIDC。
干净全量303 passed/12 skipped，真实PostgreSQL集成16 passed；
正常路径为网络HTTP，8类故障注入为ASGI HTTP+剩余真实依赖，不等于破坏真实服务。
开发HMAC身份不是OIDC；单公开合成文档闭环不是完整企业语义质量评测。

## P1：有证据的工程质量

| 证据类别 | 最低要求 |
|---|---|
| 检索 | Dense、BM25F、融合、Reranker 消融；Recall@K、MRR/nDCG、证据覆盖 |
| 生成 | Faithfulness、Citation Support、无答案识别、版本冲突样本 |
| 安全 | 零越权测试；Parent、缓存、引用、Checkpoint、撤权场景复核 |
| 可靠性 | 重复投递、Worker 崩溃、索引发布失败、LLM 超时、SSE 断连 |
| 性能 | 替身、本地真实链路、在线小样本三组；TTFT、完成延迟、P50/P95、错误率 |
| 可追溯 | 代码、模型、Prompt、索引、数据集、配置和结果可关联 |

## P2：生产演进方案

必须形成文档并写明触发条件、代价和迁移步骤：

- K8s 多副本、在线/摄取资源池和 GPU 池；
- PostgreSQL、对象存储、索引的备份恢复与 RPO/RTO；
- 多可用区、灾备、密钥管理、审计和合规；
- Embedding/Prompt/模型/索引的影子评测、灰度和回滚；
- 云上迁移（按岗位目标选择 ACK 或 EKS）；
- Incident Response、发布门禁、回滚和复盘。

## 完成状态语义

- `设计中`：只有方案，没有实现或实验。
- `已实现`：代码存在，但未完成验证。
- `已验证`：有可复现测试或实验产物。
- `已冻结`：作为当前版本基线，不轻易改变。
- `已废弃`：保留原因和替代方案。
