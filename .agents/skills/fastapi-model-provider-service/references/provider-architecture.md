# FastAPI Provider 服务架构

## 分层

```text
FastAPI routes / request schemas
  -> application service
  -> Provider protocol + capability registry
  -> concrete adapter
  -> shared async transport / SDK client
  -> upstream runtime
```

- route 负责 HTTP 验证、状态码、stream framing 和依赖注入。
- application service 负责一次调用的预算、上下文和业务结果组装。
- Provider protocol 只表达领域能力，不引用具体 SDK 类型。
- adapter 负责字段映射、传输与 normalized error。
- business fallback 位于 adapter 之外，消费结构化 `ProviderResult`。

## 最小协议

按实际需求定义 capability，例如 `chat.complete`、`chat.stream`、`embeddings`、`rerank` 或 `structured_output`。不要因为某个 SDK 有很多方法就全部暴露。

请求至少包含 model alias、输入、生成参数、request/trace context 和 deadline。结果至少包含 provider、resolved model、status、content/stream events、finish reason、usage、latency 和 normalized error。

stream event 使用显式 union，例如：

```text
start -> delta* -> usage? -> completed
                  \-> error
                  \-> cancelled
```

事件带单调递增 sequence。`completed`、`error` 和 `cancelled` 是互斥终态；网络断开后不发送新的业务事件。

## capability registry

- registry 来自服务端配置，alias 映射到 adapter、model、capabilities 和运行限制。
- 客户端只能选择允许公开的 alias，不能提交任意 base URL、SDK class 或密钥。
- 启动时检查重复 alias、缺失凭据、未知 adapter、无效默认 model 和矛盾 capability。
- 请求前验证 capability、参数范围、上下文长度与结构化输出支持情况。
- registry reload 需要版本；在途请求固定调用开始时的 resolved configuration。

## FastAPI 集成

- 使用 Dependencies 注入 request context、认证结果、registry 和 application service。
- 使用 lifespan 创建并关闭共享 HTTP/SDK client、连接池和 limiter。
- 使用 exception handlers 将领域错误映射为稳定 HTTP problem；middleware 仅处理适合所有请求的 trace、计时和安全 header。
- streaming 可使用 `StreamingResponse` 或项目既有 SSE 层，但生成器必须有 `await` 取消点并处理 disconnect。
- route 不直接捕获 SDK 私有异常；adapter 先转换为领域错误。

官方参考：

- [FastAPI Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [FastAPI Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- [FastAPI Middleware](https://fastapi.tiangolo.com/tutorial/middleware/)
- [FastAPI Custom Response and StreamingResponse](https://fastapi.tiangolo.com/advanced/custom-response/)

## deadline、timeout 与 retry

请求入口确定绝对 deadline。每个阶段按当前时间计算剩余预算，再为 queue、connect、read 和 write 分配不超过该值的 timeout。不得在 adapter、重试或 Provider 切换时重新获得完整 timeout。

retry 仅在以下条件同时满足时考虑：错误被分类为 retryable、操作可安全重放、尚未提交 stream、剩余预算足够、尝试次数未达上限。认证、invalid request、schema/protocol 错误通常不应重试。`Retry-After` 可作为输入，但仍受总 deadline 限制。

stream 首个 delta 发出后，不透明重试整次调用。若上游中断，发送或记录明确 `error/partial` 终态，由上层决定是否向用户重新发起新请求。

## 错误归一化

| 上游问题 | normalized kind | retryable 默认值 | 对外信息 |
|---|---|---|---|
| queue/connect/read deadline | `timeout` | 视操作和预算 | 稳定超时信息 |
| 429 或明确限流 | `rate_limited` | true | 可选安全 retry hint |
| 401/403 或无效凭据 | `authentication` | false | 不包含凭据细节 |
| 参数或上下文超限 | `invalid_request` | false | 可修正字段信息 |
| 5xx/运行时不可用 | `unavailable` | true | Provider alias 与安全摘要 |
| 非法 JSON/事件序列 | `protocol` | false | request ID 与协议失败 |
| disconnect/task cancel | `cancelled` | false | 明确取消状态 |

保存原始异常只允许进入受控内部诊断，并必须脱敏；对外和普通日志使用 normalized error。

## 并发与 backpressure

- 对进程总量、Provider、model 和 workload class 设置有界 limiter。
- 获取 semaphore/queue slot 也消耗 deadline，并可被取消。
- 队列满时快速返回明确 overload，而不是创建无界 task。
- streaming 在客户端消费过慢时有缓冲上限；取消后释放 slot 和连接。
- 本地模型的真实并发由基准确定，不从 CPU 核数或线程数推断。

## health

- liveness：事件循环和进程是否能服务基本请求，不调用高成本模型。
- readiness：配置、必要 client/registry 和关键依赖是否满足接收流量的条件。
- Provider diagnostic：按需或后台探测具体 Provider，结果有缓存、超时和速率限制。

单个非默认 Provider 短暂失败不一定使整个进程 liveness 失败；readiness 是否失败取决于该 Provider 是否是当前服务必需能力。

## 安全与观测

- 凭据只来自 secret store 或进程配置，不进入 request model、URL query、trace 或 error body。
- 日志记录 request/trace ID、provider alias、model alias、capability、status、error kind、latency、usage、queue wait 和 retry attempt。
- 输入输出正文默认不记录；业务确需抽样时必须有脱敏、采样、访问控制和保留周期。
- 请求携带的 tenant/actor context 由上游安全层生成，Provider 服务传播而不重新解释 ACL。
