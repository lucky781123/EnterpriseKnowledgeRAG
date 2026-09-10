# FastAPI Provider 服务验证手册

## 测试分层

| 测试 | 目标 |
|---|---|
| protocol unit | request/result/event/error 类型稳定且与 SDK 解耦 |
| adapter contract | 所有 Provider 对相同输入满足相同领域行为 |
| FastAPI integration | validation、dependency、status、header 和 error body 正确 |
| streaming | event 顺序、终态、中断、慢客户端和 disconnect |
| resilience | timeout、rate limit、unavailable、protocol error、queue full |
| lifecycle | client 只创建一次、关闭一次，无连接或 task 泄漏 |
| security | 凭据与 raw upstream body 不进入响应、日志和 trace |

## adapter contract tests

每个 adapter 复用同一测试集合：

1. complete 成功时保留 resolved provider/model、content、finish reason 和 usage。
2. stream 事件 sequence 单调，且只有一个明确终态。
3. capability 不支持时不发起网络调用。
4. timeout、429、认证、invalid request、5xx 和 malformed payload 映射到稳定错误。
5. task cancellation 关闭上游 stream 并释放 limiter。
6. adapter 不在内部切换其他 Provider，也不重置 deadline。

除不可避免的传输边界外，优先断言真实领域结果，不只检查 mock 调用次数。至少保留一个真实协议 fixture 或受控 fake server，验证 SDK/HTTP 映射。

## FastAPI 测试

- 使用 ASGI test client 或 `httpx.ASGITransport` 测试真实 route、exception handler 和 dependencies。
- 覆盖缺字段、非法参数、未知 alias、不支持 capability、请求体过大和认证失败。
- 错误响应具有稳定 code、safe message、request ID；不包含 Python traceback 或 raw SDK response。
- middleware 生成或传播 trace ID，不能覆盖可信上游 context。

## streaming 与取消

- 正常事件：`start`、多个 `delta`、可选 usage、`completed`。
- 首个 delta 前失败：返回明确错误，可以由上层决定是否重试。
- 首个 delta 后失败：终态为 `error/partial`，不能拼接第二个 Provider 的输出。
- 客户端断开：生成 task、HTTP stream、semaphore 和连接都被释放。
- 慢客户端：缓冲保持上限，服务不会无界占用内存。

## timeout 和 overload

用可控时钟或短 timeout 验证 queue、connect、read 与 total deadline。断言每次尝试的结束时间不超过原始 deadline，并检查 retry 次数。队列满时验证快速失败、指标增加和其他 Provider/model 的隔离。

## health 与生命周期

- liveness 不依赖一次昂贵模型生成。
- readiness 能发现缺失必需配置或必需 Provider 持续不可用。
- 可选 Provider 失败不会错误杀死所有流量。
- lifespan 结束后 client、connection pool、background probe 与 limiter 资源释放。

## 可观测性检查

至少验证 success、failure、cancelled、partial 四类结果的 latency、usage、queue wait、provider/model、capability、error kind、attempt 和 request/trace ID。日志测试主动放入假 API key、Authorization header 和敏感 body，断言它们均未出现。

## 验收门禁

每个注册 adapter 必须通过相同 contract suite；API、streaming、timeout、cancellation、lifecycle 和 secret tests 必须通过。报告未真实验证的 Provider 版本、SDK 行为、最大并发和长连接限制。
