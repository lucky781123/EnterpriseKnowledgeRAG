# F2-G 实验产物

本目录保存 F2-G「调用方提交 evidence 的原文摘录问答」实验记录。实验只覆盖输入契约、引用身份校验、Prompt 组装、Provider 调用、JSON/SSE 传输和有限的取消/清理观察；它不包含真实文档摄取、解析、切块、Embedding、索引、检索或 ACL 判定。

## 证据分层

为了避免把替身结果误写成真实模型结论，产物按以下层次分开：

- **离线契约层**：项目测试、静态检查和编译检查，证明代码契约与边界测试可重复。
- **Fake 基线层**：`artifacts/benchmarks/f2-g-fake-2026-09-14.json`，只测确定性的应用层路径，不代表模型延迟或容量。
- **真实本地模型层**：`local-benchmark-128.json` 与 `local-benchmark-256.json`，使用本机 LM Studio 和固定目标模型；两份结果不能与 Fake 延迟合并。
- **在线 Provider 层**：本目录没有在线调用结果；不得把未运行写成成功或失败。

## 文件说明

- `config.json`：实验范围和固定输入配置摘要，不包含密码或完整 DSN。
- `metrics.json`：面向交付和自动检查的汇总指标，引用下方两份原始 benchmark。
- `local-benchmark-128.json`：`max_tokens=128` 的完整原始结果，保留模型输出被截断的证据。
- `local-benchmark-256.json`：`max_tokens=256` 的完整原始结果，用于观察完整终态、SSE TTFT 和并发 1/2。
- `local-benchmark-128-before-cleanup.json`：修复清理顺序前的诊断产物，仅用于解释历史问题，不作为最终指标来源。
- `local-benchmark-128-wrong-model.json`：故意使用不存在模型名的环境诊断产物，不作为模型性能结果。
- `failures.jsonl`：截断等异常分类的简短记录，不保存完整模型输出或敏感配置。
- `conclusion.md`：基于原始 JSON 的边界化结论和后续限制。

## 判读规则

- HTTP 或 Provider `success` 只表示调用和协议终态完成；`answer_status`、`finish_reason` 与 `citation_validation` 必须分别阅读。
- `max_tokens=128` 的 `finish_reason=length` 是“不完整回答”证据，不是业务成功。
- `max_tokens=256` 的完整终态只证明当前 evidence 身份可校验，不证明语义正确、事实真实性、检索质量或生产可用性。
- JSON usage 可见而 SSE usage 缺失时，不从字符数、chunk 数或时间反推 token 吞吐。
- `completion_ms` 包含排队时间；SSE `ttft_ms` 只表示首个可见正文增量。
- 主动取消的 cleanup 时延只证明本地资源回收，不证明上游 GPU 已停止生成。
