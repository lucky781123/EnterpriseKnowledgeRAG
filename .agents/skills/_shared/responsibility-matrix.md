# RAG Skill 职责矩阵

| Skill | 唯一负责 | 必须协作 | 不负责 |
|---|---|---|---|
| `retrieval-pipeline-engineering` | 摄取、解析、切块、索引、查询转换、召回、融合、重排、上下文组装 | 向安全 Skill 获取授权过滤条件；向降级 Skill 报告阶段失败；向评估 Skill 暴露 trace | 身份认证、权限策略定义、Provider 切换、评测阈值 |
| `rag-acl-multitenancy-security` | tenant context、RBAC/ABAC、文档与 chunk ACL、缓存隔离、审计、安全删除、prompt injection 边界 | 为所有数据访问和 fallback 提供不可放宽的授权约束 | 检索算法调优、模型路由、质量指标实现 |
| `fastapi-model-provider-service` | Provider protocol、FastAPI API、streaming、错误映射、timeout、cancellation、health、观测 | 向降级 Skill 返回分类后的 `ProviderResult` | 业务 fallback 顺序、检索策略、ACL 策略、评测门禁 |
| `rag-degradation-and-fallback` | 故障分类、总预算、阶段预算、重试与 fallback 决策、abstain | 消费检索、授权和 Provider 的结构化结果 | 放宽授权、隐藏降级、重新实现 Provider adapter |
| `deepeval-rag-evaluation` | dataset、metric、threshold、CI gate、生产抽样、漂移与人工校准 | 消费其他 Skill 的 trace/result/decision | 在线请求实现、权限决策、fallback 执行 |

## 交叉不变量

1. `RequestContext` 的 tenant、actor、authorization version 和 deadline 必须原样传播。
2. fallback 只能保持或收紧授权，不能扩大资源范围。
3. 所有可引用证据必须保留 document、version、chunk 和 source lineage。
4. 下游阶段继承剩余时间预算，不能重新开始完整 timeout。
5. retry 和 provider/retrieval 切换必须有有限次数和明确停止条件。
6. 日志、trace、评测样本不得保存密钥、认证 token 或未脱敏敏感原文。
7. 缺少足够证据时只能澄清、abstain 或失败，不能把模型常识伪装成企业知识库证据。
8. 声称基于企业知识库的 grounded answer 必须能关联实际授权 evidence。
