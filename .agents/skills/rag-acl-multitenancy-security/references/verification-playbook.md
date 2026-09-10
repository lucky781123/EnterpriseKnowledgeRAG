# 多租户安全验证手册

## 验证原则

- 安全通过标准是攻击场景被明确阻断并留下正确审计，不是正常查询能工作。
- 跨租户、撤权和 fallback 的越权 document、chunk、Parent、cache 或 citation 命中必须为零。
- 同时测试 API、repository、索引、cache、queue consumer 和后台清理；只测 HTTP 层不足以证明隔离。
- 使用两个 tenant 的相似资源，避免内容差异掩盖过滤错误。

## 必测矩阵

| 场景 | 操作 | 预期 |
|---|---|---|
| tenant 篡改 | token 属于 A，header/query 声称 B | 忽略或拒绝，不访问 B |
| IDOR | A 用户直接传入 B 的资源 ID | 对外不泄露存在性，内部审计 deny |
| 通道不一致 | dense 与 sparse 搜索同一查询 | 两条通道越权命中均为零 |
| Parent 扩展 | child 可见、Parent 不可见 | 不扩展，最终 evidence 收紧 |
| cache collision | 两 tenant 使用相同 query | key 隔离，命中后复核 policy version |
| queue context 丢失 | envelope 缺 tenant 或签名无效 | fail closed，不用默认 tenant |
| 撤权 | cache 和索引仍有旧对象 | 权威层立即拒绝，派生数据随后清理 |
| auth 服务故障 | membership/policy 无法读取 | 不执行无 ACL 检索或模型回答 |
| prompt injection | 文档要求忽略 ACL | 当作数据，不改变系统与工具权限 |
| fallback | 检索或 Provider 失败 | 授权不放宽，证据不足时 abstain |
| offboarding | 用户或 tenant 被关闭 | session、任务、cache、索引与导出均不可达 |

## 属性与时序测试

1. tenant 替换为任意其他值，返回资源集合不能增加。
2. 移除 role/group/permission 后，授权集合只能缩小或不变。
3. 增加 policy version 后，旧 cache entry 不再命中。
4. 相同文本复制到另一 tenant 后，原 tenant 结果不变。
5. 文档插入恶意指令后，授权和工具决策不变。
6. 任一授权依赖 timeout 或报错，结果从 allow 转为 deny/error。
7. 在查询、stream、Parent 读取和 tool call 前后并发撤权，验证约定生效点。
8. 重复投递删除和撤权事件，最终状态一致且不会重新创建可访问数据。

## 审计验证

- request/trace ID、actor、tenant、operation 与 resource identity 可关联；
- decision、policy version 和稳定拒绝原因正确；
- fallback、cache 和检索通道可追踪；
- 不包含 authorization header、cookie、API key、完整 token 或无必要正文；
- 审计存储自身具备访问控制、完整性和保留周期。

## 删除证明

删除测试不能只检查 API 成功，还要检查业务事实源、原始对象、解析产物、dense/sparse 索引、Parent、cache、snapshot、引用、评测样本、导出物，以及仍在排队或运行的任务。存在合规备份时，记录范围、加密、访问权限和最终销毁时间。

## 发布门禁

上线前提供授权矩阵、数据流图、跨租户测试、撤权/删除演练、日志脱敏检查、依赖故障测试、prompt injection 测试与剩余风险。任何跨租户泄漏、fail-open 或 fallback 绕权都是阻断级问题。
