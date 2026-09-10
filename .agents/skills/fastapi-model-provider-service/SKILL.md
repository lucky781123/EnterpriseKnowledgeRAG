---
name: fastapi-model-provider-service
description: Use when designing, implementing, testing, or operating a FastAPI service that exposes model providers through stable async, streaming, health, error, timeout, cancellation, concurrency, and observability contracts. Do not use to choose business fallback order, retrieval strategy, authorization policy, or RAG quality thresholds.
---

# FastAPI Model Provider Service

## 目标

用稳定的领域协议隔离 FastAPI、上游 SDK 和具体模型运行时。API 调用方只依赖统一 request、stream event、`ProviderResult` 与错误分类，不感知 Provider 私有响应结构。

## 开始前

1. 检查已安装的 FastAPI、Pydantic、HTTP client 和 Provider SDK 版本及现有代码模式。
2. 明确实际需要的 capability、同步或异步边界、streaming 格式、并发、deadline、认证和兼容性要求。
3. 列出上游 Provider 的 timeout、rate limit、认证、协议和取消行为，不凭名称猜测兼容性。
4. 从 [共享职责矩阵](../_shared/responsibility-matrix.md) 获取 Provider 与 fallback 的边界。
5. 先写 Provider contract、API error、stream interruption、disconnect 和 health 测试。

## 选择性读取

- 设计 protocol、adapter、registry、streaming、deadline 或 health 时，读 [provider-architecture.md](references/provider-architecture.md)。
- 编写测试、故障注入或上线验收时，读 [verification-playbook.md](references/verification-playbook.md)。
- 跨模块输出使用共享 `RequestContext` 和 `ProviderResult` Schema。

## 核心工作流

1. **定义领域协议**：为 request、result、stream event、capability 和 normalized error 建立与 SDK 无关的类型。
2. **实现 adapter**：每个 Provider 只做字段转换、传输、错误映射、usage 与 finish reason 提取。
3. **建立 registry**：通过服务端配置的 alias 选择 Provider 和 model，并在调用前检查 capability。
4. **建立 FastAPI 边界**：Pydantic 验证请求，Dependencies 注入 registry/context，exception handler 统一安全错误。
5. **控制生命周期**：在 lifespan 中创建和关闭共享 client，配置连接池、有界并发和 backpressure。
6. **传播预算与取消**：从请求 deadline 计算剩余预算；客户端断开、服务取消或 timeout 必须向上游传播。
7. **定义 streaming**：使用显式 event 类型、序号、终止或错误事件；首个事件发出后不做透明重试。
8. **观测与验收**：记录 request/trace、provider alias、model、capability、状态、latency、usage 和安全错误类型。

## 必须保持

- FastAPI schema、领域 protocol 和 Provider SDK model 分层，避免 SDK 类型穿透业务代码。
- capability 在网络调用前验证；不支持的操作返回稳定错误，而不是依赖上游偶然失败。
- 所有 queue、connect、read、write 和 total timeout 不超过请求剩余 deadline。
- 共享 client 在 lifespan 内复用并正确关闭；不能每次请求新建连接池。
- retry 只针对明确可重试且尚未向客户端提交 stream 的操作，并有次数与预算上限。
- 客户端 disconnect 和 task cancellation 传递到 semaphore、HTTP stream 与 Provider SDK。
- 原始上游异常被映射为稳定错误类别；响应和日志不暴露密钥、header 或敏感 body。
- liveness 只证明进程可运行；readiness 反映是否能接收目标流量，两者不能混用。
- adapter 不决定跨 Provider fallback 顺序，不重置 deadline，也不伪造正常结果。

## 禁止行为

- 不允许客户端提交任意 Provider base URL、密钥或未注册 model endpoint。
- 不捕获所有异常后统一返回 `500 unknown`，也不把 raw upstream message 原样返回。
- 不在流已发送部分内容后从头切换 Provider 并拼接两个输出。
- 不用无界 task、全局锁或无限等待实现并发控制。
- 不让 health endpoint 执行高成本生成，或把一次瞬时 Provider 故障当作进程死亡。
- 不在 adapter 内实现检索、ACL、fallback 决策或评测门禁。

## 完成标准

提供 protocol、adapter contract、capability registry、API schema、error mapping、stream event、lifespan、timeout/cancellation、health 和 observability 设计。对每个 adapter 运行相同 contract tests，并报告流式中断、取消、限流、协议错误和敏感信息测试结果。
