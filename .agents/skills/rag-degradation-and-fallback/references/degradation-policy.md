# RAG 降级与 Fallback Policy

## 决策输入

policy 不读取 raw SDK exception。输入应包含：

- `RequestContext`：request/trace、tenant/actor、authorization version、绝对 deadline；
- 当前 stage 与结构化 failure kind；
- retryable、idempotent、attempt、cost、token、queue wait；
- 是否已经向客户端提交 stream 或副作用；
- 当前 retrieval candidates/evidence 是否充分且仍授权；
- 已访问的 graph node、circuit 状态和可用 capability。

输出使用 `FallbackDecision`，明确 action、reason、attempt、max attempts、remaining budget、`preserves_authorization`、`evidence_sufficient` 和用户模式。

## 决策优先级

按顺序检查，前项失败时不继续寻找更“可用”的路径：

1. request 已取消或 deadline 耗尽：cancel/fail。
2. tenant、认证、授权或 policy context 无效：fail closed。
3. invalid request、schema、capability 或非幂等副作用失败：fail，不 retry。
4. 已提交部分 stream：标记 partial/error，不透明切换并拼接。
5. evidence 不足或冲突不可解决：abstain/fail。
6. attempt、cost、token、并发或 circuit 限制达到上限：abstain/fail。
7. 只有在候选边的 capability、授权、证据与预算前置条件都满足时执行一次 fallback。

## 预算模型

使用绝对 deadline 而不是每层相对 timeout。推荐记录：

```text
remaining_time = deadline_at - now
remaining_attempts = max_attempts - attempts_used
remaining_cost = max_cost - cost_used
remaining_tokens = max_tokens - tokens_used
```

queue、connect、read、retrieval、rerank 和 generation 的阶段 timeout 都不超过 `remaining_time`。为返回错误、释放资源和发送终态保留小额收尾预算。切换 Provider 或检索通道时不重置任何总量。

## Fallback graph

把策略建模为有限有向无环图，而不是嵌套 `try/except`：

```text
hybrid retrieval
  -> dense-only degraded   [sparse failed AND evidence sufficient]
  -> sparse-only degraded  [dense failed AND evidence sufficient]
  -> abstain               [authorization invalid OR evidence insufficient]

reranked candidates
  -> base-rank degraded    [reranker failed AND base rank accepted by policy]
  -> abstain               [critical ranking requirement not met]

primary provider
  -> secondary provider    [before stream commit AND compatible AND budget remains]
  -> partial/fail          [after stream commit]
```

每条边声明允许 failure kinds、最大使用次数、兼容 capability、质量最低条件、授权条件、预计成本和 timeout。启动时验证无环、目标 node 存在、没有无法到达的 node，也没有绕过安全 gate 的边。

## 检索降级

| 失败 | 可考虑动作 | 必要条件 |
|---|---|---|
| dense 后端不可用 | sparse-only | 相同 ACL/version filter；期望 evidence 命中且质量门槛满足 |
| sparse 后端不可用 | dense-only | 相同 ACL/version filter；精确标识符类 query 不被错误回答 |
| Reranker timeout | base rank | 候选已授权；离线评估允许该 query 类别降级 |
| query rewrite 失败 | original query | 原问题有效；未超过预算；模式明确 |
| Parent 扩展失败 | child-only 或 abstain | child 自身足够、授权且可引用 |
| 所有 retrieval 失败 | abstain/fail | 不允许纯模型伪装成知识库答案 |

“有候选”不等于“证据充分”。充分性由可观察规则或已校准 evaluator 判定，不能让生成模型在同一次回答中自证。

## Provider 降级

- primary 与 secondary 必须支持请求需要的 capability、上下文长度、stream 格式和合规区域。
- 参数映射由 Provider adapter 完成，policy 只选择已注册 alias。
- 认证和 invalid request 不通过切换 Provider 掩盖。
- rate limit、unavailable 或 timeout 是否切换取决于剩余预算、幂等性、circuit 和是否已提交输出。
- 不同模型的行为和质量差异必须在用户模式、trace 和评估标签中可见。

## cache fallback

只使用可验证来源的 cache entry。至少匹配 tenant、授权 scope 或 actor/group hash、policy version、index/version、query、retrieval config、model/prompt version、内容 hash 和 TTL。读取后重新确认资源可见性及 lineage。

semantic answer cache 风险高：相似问题可能具有不同权限、时间语义和答案边界。除非有独立验证证据，否则优先缓存检索派生物或精确键结果，而不是复用最终答案。

## circuit breaker

- breaker 以 Provider/model/operation 或依赖实例为范围，避免一个故障关闭所有能力。
- 只根据符合定义的失败计数，客户端 invalid request 不应触发依赖 circuit。
- open 状态快速拒绝新调用，减少排队和级联故障。
- half-open 探测有低并发、短 timeout 和独立观测；不能让正常流量形成探测风暴。

## 用户模式与观测

对外文字不必暴露内部拓扑，但必须诚实区分：正常、降级、部分、无足够证据和服务失败。引用只来自实际 evidence，不能因降级省略来源要求。

记录 source node、target action、failure kind、attempt、remaining budgets、circuit、授权版本、evidence sufficiency、user mode、latency、cost 和 outcome。不要记录密钥或无必要正文。

## 实现建议

将 policy 写成尽可能纯的决策函数：同一输入得到同一 `FallbackDecision`。执行器只执行被选择的一步并返回新状态，再由 policy 重新评估。这样能独立做 graph、属性和故障注入测试，也能防止执行器私自增加 retry。
