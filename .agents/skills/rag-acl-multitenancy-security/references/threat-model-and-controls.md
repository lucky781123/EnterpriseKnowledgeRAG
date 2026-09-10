# RAG 多租户威胁模型与控制

## 资产与信任边界

| 资产 | 主要风险 | 最小控制 |
|---|---|---|
| 原始文档与版本 | 跨租户读取、旧版本残留 | tenant ownership、发布状态、ACL、撤权优先 |
| chunk 与 embedding | ACL 丢失、相似文本误合并 | 每个派生物继承 resource identity 与 policy metadata |
| dense/sparse 索引 | 过滤不一致、共享 collection 泄漏 | 查询前强制 tenant/ACL/version filter，结果复核 |
| cache | key 碰撞、权限变化后陈旧命中 | tenant、actor scope、policy/index/prompt version 入 key |
| queue 与后台任务 | tenant context 丢失、任务混淆 | 服务端签发 envelope、消费者复核、租户级配额 |
| Prompt 与工具 | 检索内容覆盖规则、越权工具调用 | 内容与指令分离，工具侧独立授权 |
| 日志与评测 | token、正文、tenant 信息泄漏 | 最小字段、脱敏、访问控制和保留周期 |

攻击者包括：普通租户用户、被撤权用户、恶意文档作者、被攻陷的客户端、错误配置的内部服务，以及拥有部分运维权限但无业务数据权限的操作者。

## 授权决策

一个资源可读至少同时满足：

```text
identity 已验证
AND actor 当前属于 tenant
AND request tenant 来自可信上下文
AND resource.tenant_id == request.tenant_id
AND resource version 已发布且未撤权
AND RBAC/ABAC 允许该操作
AND document/chunk ACL 允许 actor、role 或 group
AND policy version 仍然有效
```

认证回答“是谁”，成员关系回答“属于哪个 tenant”，授权回答“能对哪个资源做什么”。不要把三者合并成一个布尔字段，也不要用 HTTP `tenant-id` header 覆盖 token 和服务端成员关系。

## tenant context

- 验证 token 签名、issuer、audience、expiry 和授权用途。
- 从 token subject/session 找到服务端成员关系；多 tenant 用户必须显式选择且再次校验目标 tenant。
- service account 使用最小 scope，并区分代表用户执行与系统后台执行。
- 管理员跨 tenant 操作使用独立入口、显式 target tenant、强认证和完整审计，不能复用普通查询路径的隐藏开关。
- 数据访问 API 不接受对象自身携带的 tenant 来扩大当前上下文。

## 存储隔离

隔离强度按风险选择：独立数据库/collection、独立 schema/namespace、共享表加行级约束。共享模式仍需多层防护：

1. 数据层每条记录包含 tenant 与 resource identity。
2. repository/query builder 自动注入 tenant 过滤，调用者不能选择跳过。
3. 数据库 RLS 或等价机制作为第二层保护。
4. 唯一键、外键和 upsert 冲突域包含 tenant。
5. 备份、导出、临时文件、对象存储 path 和批处理目录同样隔离。

向量库的 metadata filter 不是 ACL 的唯一事实源。权限变化后应先在权威层取消可见性，并让旧索引、缓存和 snapshot 失效。

## ACL 传播和检索

- ingestion 时从权威资源生成 ACL metadata，并复制到每个 chunk；不能只保留 document title 或路径。
- dense、sparse、metadata 和 hybrid 每条通道在检索前应用等价过滤。
- 结果进入融合、Reranker、Parent 扩展、context、引用详情前再次确认 resource 与 authorization version。
- post-filter 只用于防御索引陈旧和实现缺陷；如果 pre-filter 不存在，候选数量、时间和日志仍可能泄漏资源存在性。
- `forbidden` 与 `not found` 的对外响应避免暴露资源是否存在；内部审计保留真实决策。

## cache、queue 和异步任务

推荐 cache key 至少包含：tenant、授权 scope 或 actor/group hash、policy version、index/version、query hash、retrieval config、model/prompt version。命中后仍检查授权版本与资源可见性。

队列 envelope 至少包含 request/job ID、tenant、actor 或 service identity、operation、resource IDs、policy version、issued-at 和 integrity protection。消费者重新加载当前成员关系和资源状态；不能信任生产者传来的 `authorized=true`。

租户级并发、队列深度、存储和 token 配额用于限制 noisy neighbor。配额拒绝不得切换到不计租户的全局队列。

## 撤权、删除和 offboarding

1. 权威层先撤销访问或标记资源不可见。
2. 增加 authorization/policy version，立即使缓存和旧结论失效。
3. 取消或阻断未开始的后台任务；运行中任务在下一个访问点重新验证。
4. 级联删除或重建 dense/sparse 索引、Parent、摘要、缓存、评测样本和导出物。
5. 记录每个派生系统的清理状态并允许幂等重试。
6. 用查询和存储检查证明资源不再可达，而不是只证明删除任务已提交。

已发送给客户端的流式内容无法撤回，因此必须定义在途请求语义。高敏感场景应在流式 chunk、工具调用或 checkpoint 处重新检查短期授权租约。

## prompt injection 边界

- 文档、网页、issue、代码注释和工具输出都是不可信数据。
- 用清晰分隔和结构化字段区分 system instructions、用户问题与 evidence。
- evidence 中出现“忽略规则”“暴露秘密”“调用工具”时，不改变授权、工具列表、系统策略或输出数据范围。
- 工具/API 自己执行 tenant 与资源授权，不能依赖模型决定参数是否可访问。
- 检测器和内容过滤可降低风险，但不能代替确定性访问控制。

## 审计最小字段

记录 timestamp、request/trace ID、actor、tenant、operation、resource identity、decision、policy version、拒绝原因类别、检索通道、fallback mode 和 outcome。对 query、文档正文、token、密钥、PII 与模型输出做分级脱敏和保留控制。

## 主要参考

- [OWASP RAG Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)
- [OWASP Multi Tenant Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html)
