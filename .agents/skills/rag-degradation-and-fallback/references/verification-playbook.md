# 降级与 Fallback 验证手册

## 静态验证

- graph node 和 edge ID 唯一，目标存在，整图无环。
- 每条 retry/switch edge 有 failure kind、max uses、预算、capability、授权和证据前置条件。
- authorization、cancel、deadline、invalid request 与 stream committed 有明确终止规则。
- 没有从授权失败、证据不足或 circuit open 指向正常回答的边。

## 属性测试

1. attempt、cost 或 elapsed time 增加时，可用 fallback 集合不能扩大。
2. permission 减少或 policy version 改变时，evidence 集合只能缩小或重新验证。
3. `evidence_sufficient=false` 时 action 只能是 `abstain` 或 `fail`。
4. `stream_committed=true` 时不能透明 retry 或切换并拼接。
5. 同一 node/edge 的使用次数不超过声明上限。
6. 任意执行序列都在有限步内到达 completed、partial、abstained 或 failed。
7. 所有 `FallbackDecision.preserves_authorization` 必须为 true。

## 故障注入矩阵

覆盖 retrieval channel、Reranker、Provider、cache、authorization dependency、queue、circuit probe 和 telemetry 的 timeout、rate limit、unavailable、protocol error、cancel 与 partial output。每个场景同时检查 action、总耗时、调用次数、用户模式、授权范围和最终 evidence。

## 安全与证据测试

- 两个 tenant 使用相同 query 和 cache key 材料，不能交叉命中。
- 备用检索和 Provider 收到同一 `AuthorizationContext` 和剩余 deadline。
- policy version 变化后，旧 cache 与旧 retrieval trace 不能直接复用。
- 所有检索失败时，输出不得引用不存在的企业文档或声称基于知识库。
- 恶意文档提出 fallback 指令时，graph 与工具权限不发生变化。

## 部分输出测试

在第一个 delta 前、首个 delta 后、中途 usage 和完成事件前分别注入故障。检查是否允许 retry、是否产生重复 token、终态是否唯一、引用是否只覆盖已确认 evidence，以及连接和 limiter 是否释放。

## 生产指标

至少按 stage、failure kind、source/target node、Provider/model、retrieval mode 和 tenant tier 聚合：正常成功率、降级成功率、abstain、partial、失败、retry、circuit open、额外延迟、额外 token/成本、cache 使用率和安全阻断数。

高 fallback 成功率也可能隐藏主依赖长期损坏。为 fallback rate、连续降级时长和质量差异设置告警，并通过 evaluation Skill 监控 normal 与 degraded mode 的独立指标。

## 发布门禁

交付 graph 可视或结构化定义、每条边的正反测试、预算属性测试、跨租户与证据不足测试、partial stream 测试、circuit 演练和用户模式样例。任何无限循环、deadline 重置、绕权、伪造证据或静默降级都阻断发布。
