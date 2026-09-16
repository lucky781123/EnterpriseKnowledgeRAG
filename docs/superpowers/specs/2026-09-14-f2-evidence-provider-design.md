# F-2：Evidence Bundle 与真实模型服务设计

- 日期：2026-09-14
- 状态：用户已选择 F-2；以下详细契约和默认参数为待审阅草案，尚未实施。
- 基线：`1667676`。PostgreSQL 基础与部署切片沿用前序已验证结果。
- 规划归属：`.planning/2026-09-09-enterpriseknowledgerag-development/`。

## 1. 目标与边界

完成“调用方提交问题和证据 -> 服务端校验 -> Prompt 组装 -> 真实 LM Studio -> JSON/SSE 答案与引用”的可运行链路，并交付契约测试、生成侧评测、有界 benchmark 和复现文档。

这是一条 Evidence Bundle 生成链路，不是完整 RAG。调用方可以提交自备文本，不限于服务端 fixture。服务端不抓取 source URL，不按文档 ID 读取数据库或文件，不核验来源真实性，不把调用方声明当作身份、租户权限或已完成的检索。

用户已在候选中选择 F-2：相较纯 Provider 入口，它保留 Prompt 和引用契约；相较双层 API 或多模式入口，它只维护一条生成路径。代价是调用方负责准备证据，无法验证上游检索过程；未来接入真实检索时需重新确认资源授权和 lineage，不能自动继承调用方声明。

本阶段不实现上传、parser/chunker、摄取持久化、Embedding、Weaviate、Dense/BM25F、trace replay、OIDC/ACL、队列、跨 Provider fallback 或在线模型调用。项目原有摄取和检索目标保留为后续任务，不因 F-2 被标记完成。

## 2. API 输入与输出

拟新增两个入口，共用同一个 application service：

- `POST /api/v1/query`：返回完整 JSON。
- `POST /api/v1/query/stream`：相同请求模型，返回 SSE。

不增加纯 messages 入口、fixture_id 模式或 context_mode 多分支。

请求示例：

```json
{
  "question": "API 就绪检查是否负责执行数据库迁移？",
  "evidence": [
    {
      "evidence_id": "doc_1",
      "text": "数据库迁移由部署阶段的 one-shot job 执行，API readiness 只检查版本。",
      "source": "调用方提供的部署说明"
    }
  ],
  "model_alias": "local_answer",
  "temperature": 0.0,
  "max_tokens": 512,
  "timeout_ms": 120000
}
```

拟定初始限额是工程保护值，不是已测性能结论：

| 字段/资源 | 规则 |
|---|---|
| 请求体 | 原始请求体最多 128 KiB，按实际读取字节计数，不能只信 Content-Length |
| question | 去除首尾空白后非空，最多 4096 个 Unicode 字符 |
| evidence | 1 至 16 条；空数组拒绝，不暗中改成纯模型回答 |
| evidence_id | 1 至 64 个 ASCII 字母、数字、下划线或连字符；请求内唯一 |
| text | 非空；每条最多 8 KiB UTF-8，全部 text 合计最多 32 KiB |
| source | 可选非空说明，最多 512 字符；仅展示来源声明，不作为路径或抓取目标 |
| model_alias | 仅服务端已注册的 `local_answer`；Fake 只通过测试依赖注入 |
| temperature | 有限数值，0 至 1，默认 0 |
| max_tokens | 整数 1 至 2048，默认 512 |
| timeout_ms | 整数 1000 至 300000，默认 120000；调用方不能扩大服务端上限 |
| 额外字段 | 拒绝，尤其是 base_url、api_key、tenant_id、authorized、retrieval_trace |

严格验证数值类型，拒绝 NaN、Infinity、布尔冒充整数。上下文不静默截断、不重排、不自动丢条目。上述字节限额用于限制服务资源，不等于模型的精确 token budget；真实 tokenizer 或运行时上下文窗口未验证前不承诺一定装得下。上游明确报上下文超限时归一化为 `invalid_request`。

响应包含 request_id、trace_id、`context_source=caller_supplied`、答案、citations、citation_validation、answer_status、ProviderResult 和 timings。ProviderResult 单独遵守共享 Schema，不向其添加 Schema 禁止的额外字段。usage 未提供时为 null，不伪造零值；timings 中未到达的阶段同样为 null。

`answer_status` 限定为 `answered`、`abstained`、`invalid_citations`、`incomplete`、`failed`、`cancelled`。Provider 的 success 仅表示模型调用成功，不代表答案通过引用或质量验收。

`citation_validation` 限定为 `passed`、`failed`、`not_applicable`、`not_checked`；尚未完整生成或被取消时为 `not_checked`，不能将未执行检查写成通过。

## 3. Prompt 与引用语义

服务端使用版本化 system Prompt，要求只根据本次 evidence 回答；资料不足或冲突时说明限制。问题和 evidence 用结构化 JSON 序列化放入 user 消息，不通过拼接角色标记赋予 evidence 指令权限。source 同样是不可信文本。

按照请求顺序给 evidence 分配内部标签 E1 至 En，并要求模型用 `[[E1]]` 格式引用；返回结构化 citation 时映射回调用方的 evidence_id。调用方 ID 不直接作为 Prompt 指令或分隔符。身份校验不抓取外部资源。

- 正常回答至少有一个格式完整且存在于本次 bundle 的引用。
- 未知标签、残缺标记或非拒答输出没有引用：`answer_status=invalid_citations`，`citation_validation=failed`。
- 用固定前缀 `资料不足：` 表达拒答；结构符合时 `answer_status=abstained`，引用状态为 `not_applicable`。是否真正应该拒答属于质量评测，不能仅凭该前缀认定模型正确。
- 合法引用去重并保留首次出现顺序；citation 只包含 label、evidence_id 和可选 source，不额外回传整段证据。
- 非成功 Provider 终态或 finish_reason 表示长度截断时，不标记完整回答；保留 `incomplete` 和明确原因。
- JSON 中引用失败保留模型原输出以便诊断，但通过状态明确其不是可接受答案；不伪造引用、不偷偷重试或补全模型的事实。

引用校验仅验证格式与 identity，不验证事实支持、来源真实性、文档版本或访问权限。Prompt 分隔也不等于消除了 prompt injection。

## 4. SSE 契约

沿用项目 `stage/tool_call/token/citation/error/done` 事件集合；F-2 没有真实工具调用，因此不发 tool_call。不增加 start、cancelled 等新事件名。

每个业务事件携带 request_id 和从 1 开始的单调递增 seq。token 表示可显示文本增量，并不等于 tokenizer 的一个 token。

正常序列：

```text
stage(validated) -> stage(queued) -> stage(generating)
  -> token* -> stage(validating_citations) -> citation* -> done
```

stage 仅发模板化状态，不含真实内部推理。citation 在最终引用检查完成后发送；失败时不发“已通过校验”的 citation。

- 请求体、alias、capability 和入队准入错误在提交 SSE 响应前返回 JSON 非 2xx。
- SSE 响应已提交后，HTTP 状态保持不变；在连接可写且剩余发送预算允许时，业务失败发至多一个 error，再发一个 done；总预算耗尽或发送受阻时直接关闭并记录失败，不为发送终态重新获得一份 deadline。
- done 是唯一完成标记，携带 answer_status、citation_validation、ProviderResult 元数据和 timings，不重复传输全文；每个流至多一个 done，其后不再发事件。
- token 是尚未最终验收的增量。已发送内容无法撤回；最终引用失败时发送明确 error/done，不能让消费者仅凭已有 token 推断成功。
- 客户端断开后不再尝试发送事件，仅记录 cancelled 并释放资源；客户端没有收到 done 时应按未完成处理。
- 单独的上游 reasoning/reasoning_content 不传给客户端或写入普通日志。真实模型若把推理混入正文，须先验证其运行时配置与输出格式；未解决前不宣称满足不输出推理的验收条件。
- malformed JSON、异常事件序列、未完整结束的上游流都进入协议失败路径；不能把 EOF 一律当作成功。

## 5. 分层与 Provider

```text
FastAPI schemas/routes
  -> EvidenceAnswerService（输入、Prompt、引用、业务结果）
  -> LocalLLMScheduler（并发、入队、deadline）
  -> ChatProvider（领域类型、capability、标准结果与增量）
  -> LMStudioChatProvider（HTTP 字段映射与错误归一化）
  -> lifespan 共享 httpx.AsyncClient
```

沿用现有 providers 包和 Settings。通过 Settings 在启动时构建最小 alias registry，不引入 YAML loader、热更新框架或通用插件系统；model-registry.yaml 继续作为能力与验证记录，并在交付时同步真实结果。

保留 ChatRequest/ChatResponse 的既有意义，按必要语义增加 finish_reason、usage 和结构化内部流事件。同步调整 Fake、scheduler 与受影响测试，不保留两套用途相同的协议；修改范围须覆盖全部当前调用方。

首期 capability 仅 chat.complete/chat.stream。工具调用、视觉和 structured_output 不因模型名称或兼容接口而默认开放。adapter 只调用配置好的根地址下 `/v1/chat/completions` 与只读模型诊断接口；禁止双重 `/v1`、跨站重定向和自动读取系统代理转发本地正文。

应用 lifespan 创建共享 HTTP client、registry 和 scheduler，退出时关闭在途流和 client，并继续释放原数据库连接池。不得每个请求创建 client，也不得在模块 import 阶段发起网络调用。

## 6. Deadline、取消与错误

默认复用本地并发 2，增加最多 8 个等待请求；等待超过准入上限立即拒绝，不创建无限等待任务。入队等待最多 10 秒并计入总预算。HTTP 连接/写入/连接池等待上限分别为 5/10/5 秒，读取空闲上限为 60 秒，全部受剩余总预算约束。

总预算从入口接收请求开始计时，用 monotonic clock 控制，覆盖请求读取、排队、生成、流式发送和最终校验。不能以“每次收到字节重置 read timeout”取代总 deadline。取消等待请求不释放尚未获得的 semaphore；持有槽位的请求在所有退出路径恰好释放一次。

使用按需拉取与有界解析缓冲，避免创建无界生产者队列。拟定上游单个 SSE frame 上限 64 KiB、整个上游响应最多 1 MiB、可显示答案最多 64 KiB；超限中止并关闭上游，不继续累计。慢客户端受总 deadline 限制，测试须覆盖阻塞发送时的取消和清理。

本阶段自动重试次数为 0；retryable 仅保留分类语义，不触发隐藏请求。中途失败不切换 Provider，不将两次生成拼接。

| 情形 | 对外处理（尚未提交 SSE 时） |
|---|---|
| 输入/alias/capability 不合法 | 422、invalid_request |
| 原始请求体超限 | 413、invalid_request |
| 等待队列满/上游 429 | 429、rate_limited |
| deadline/网络 timeout | 504、timeout |
| 上游认证失败 | 502、authentication，不能误导为调用方需要提交模型密钥 |
| 上游断连或 5xx | 503、unavailable |
| 上游格式或非支持输出错误 | 502、protocol |
| 完整生成后引用检查失败 | 200、answer_status=invalid_citations；ProviderResult 仍表达实际调用状态 |
| 未预料内部异常 | 500、安全 unknown；不能吞掉可分类异常 |

取消使用共享 ProviderResult 的 cancelled 状态和 cancelled 错误分类。对已经发出文本的上游错误记录 partial；主动取消保持 cancelled，已发文本数量在外层观测字段记录。

证明“应用 task 取消、HTTP 流关闭、槽位释放”不等于证明“LM Studio GPU 停止生成”。后者单独记录为 observed/unverified，不做虚假保证。

## 7. Health 与安全

`/health/live` 不调用模型。保留 `/health/ready` 的数据库和目标 migration 检查；新增启用的默认 Provider readiness 摘要，不降低原数据库条件。Provider 只读探测设 2 秒超时和 5 秒缓存，同一进程合并并发探测，不做生成或模型下载。

模型列表可达不等于推理正常；health 明确区分配置/端点可达与真实生成 smoke。只读探测不能证明模型已加载或具有足够上下文窗口。

F-2 不读取受保护的服务端数据。生成本地调用级 request/trace/deadline context；不把调用方输入转换为共享 RequestContext 中的可信 tenant/actor/authorization version，也不填充假身份声称通过授权。未来接入企业数据时必须补真实身份来源和授权，不能直接扩展当前入口读取资源。

本阶段仅用于受控本机开发；运行命令绑定 loopback，Compose 主机映射如有相关改动也限定 loopback。不宣称提供公网或多用户安全服务。CORS 默认不开放，接收 JSON，不配置在线 fallback。

API 错误、Pydantic 验证错误和普通日志只记录安全字段，不包含输入值、正文、完整上游异常、DSN、密钥或 Authorization header。诊断保留 request_id、alias、error kind、阶段、长度、延迟和 usage。

## 8. 测试与证据

| 层次 | 必须覆盖 | 能证明的内容 |
|---|---|---|
| Fake | 输入边界、Prompt、引用、状态、预算、队列、取消、流终态 | 确定性应用契约 |
| mock HTTP | complete/stream、碎片与多行帧、UTF-8、usage 缺失、reasoning 字段、401/429/5xx、非法 JSON、EOF、超时与清理 | 本项目对已建模上游响应的处理 |
| 真实 LM Studio | 模型发现、JSON、SSE、finish reason、引用、usage、断开后的清理 | 当前本机与模型的已观察行为 |
| 本机 HTTP 端到端 | 通过真实 socket 验证慢消费者、断开、deadline 与 lifespan | 不依赖 TestClient 缓冲的网络生命周期行为 |

新增一个小型、自包含、版本化 F-2 smoke 数据集：可回答、证据不足、版本冲突、注入文本和非法输入。全部明确为合成或项目公开材料，不从未确认的 40 条草案推断真实检索命中；原草案保留不动。

生成侧评测输出 actual_output、实际提交 evidence 的版本/hash、引用 identity 结果、拒答/冲突样本结果与人工待复核标记。确定性规则进入 EvaluationObservation；不执行的语义指标或检索指标在报告注明 not_applicable/not_run，不伪造 score=0 或 passed=true。

质量要求与运维要求分开：结构/identity/security 控制可做确定性 gate；事实支持、回答正确性和冲突识别需要人工复核后才能下结论。本阶段不引入未校准的 DeepEval Judge 或收费在线评估依赖，也不宣称完整质量评测已经完成。

## 9. 有界 Benchmark

runner 复用应用入口，分别报告 Fake 与真实 LM Studio 的 JSON/SSE，不混合延迟分布。初始真实配置为客户端并发 1、2，每组 10 次，每次输出上限 128 tokens，每组总时限 10 分钟；1 次 warmup 单独记录。默认不自动执行在线调用，不把模型输出设为精确确定性。

另设 2 次主动取消实验，与正常组分开；取消产生的未完成响应不能计入成功延迟样本。超时和错误都进入分母，不只留下成功请求。

- queue_ms：进入队列至获得槽位。
- TTFT：请求开始至首个可见非空正文增量；不是首个 stage、HTTP 字节或 reasoning 字段。JSON 模式不报告可见 TTFT。
- completion_ms：请求开始至完整终态；截断、取消、错误的终止耗时单独报告。
- 成功延迟的 P50/P95 使用固定 nearest-rank 算法，并同时报告样本数、原始数值和失败计数；10 条样本不足以支持稳定容量推论。
- 错误率、取消率、截断率、完成吞吐分别报告；usage 缺失不推算精确 token 吞吐。
- 取消清理时延、任务/连接回收证据，与上游 GPU 停止证据分开。

产物记录代码 commit 与 dirty patch hash、数据/Prompt/配置 hash、Provider 与模型、运行时版本、并发、限额、开始结束时间和命令。index/embedding/reranker 标记 not_applicable。资源/功耗/费用没有实际测量时标记未测量；不得根据客户端进程内存推断整个模型峰值。

## 10. 交付与验收顺序

1. 审阅本设计后生成详细实施计划；先写失败测试，再实现协议与输入/引用边界。
2. 接入真实 adapter 与 scheduler；验证超时、取消、错误、缓冲和 client 生命周期。
3. 接入 JSON/SSE 与 health；运行离线回归和本机 socket 故障实验。
4. 运行真实 LM Studio smoke 和有界 benchmark；不可达时记录环境缺口，不将 skip 当作通过。
5. 交付 ADR、复现说明、生成侧评测报告、benchmark 报告，并同步 PROJECT_CONTEXT、主计划和交接文档。

变更主要落在 providers、api、一个小型 evidence application service、必要 Settings、evaluation/runner、对应 tests 与 docs/artifacts。未发现必要性前不修改 db、storage、reranking、共享技能或 schema，不添加新基础设施。

新增代码与测试使用详细中文注释，重点说明输入/输出契约、不变量、信任边界、取消清理和失败原因，不用逐行翻译代替解释。

## 11. 当前已知与待验证

- 已知：用户选定 F-2；当前代码没有真实 Chat HTTP adapter 或 query/SSE；基础依赖已安装。
- 已知：PostgreSQL 前序验证已完成，无需再次重置密码或删除 volume。
- 待审阅：本文件的具体 API、限额、终态与质量验收口径。
- 待验证：本机 LM Studio 是否运行、指定模型是否可调用、推理是否与正文分离、输出预算与实际延迟是否匹配。
- 非阻塞的后续项：生产认证、完整 RAG、语义 Judge 校准、线上容量。它们未被本设计宣称完成。

## 12. 官方依据

查阅日期为 2026-09-14。HTTPX 正文已核对；LM Studio 页面本次读取返回 HTTPError，保留参考入口，接口与本机兼容性待实施前核对。这些资料均不能代替本机运行证据：

- LM Studio Chat Completions：`https://lmstudio.ai/docs/developer/openai-compat/chat-completions`
- HTTPX Async Support：`https://www.python-httpx.org/async/`
- HTTPX Timeouts：`https://www.python-httpx.org/advanced/timeouts/`
