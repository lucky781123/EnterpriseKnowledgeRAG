# F2-G 评测说明

## 评测范围

F2-G 只验证“调用方提交 `question + evidence` 后，服务完成输入校验、Prompt 组装、模型调用、摘录身份校验和 JSON/SSE 终态”的生成侧契约。当前没有真实摄取产物、向量索引或检索结果，因此本文件不把调用方提供的 evidence 当作检索召回证据，也不把引用身份校验当作事实正确性。

## 已执行的契约测试

- 确定性 evidence identity 校验：覆盖空证据、重复 ID、未知 ID、越界位置、重复文本歧义和 Unicode 字符索引。
- Provider mock contract：覆盖 `/v1/chat/completions`、usage、`finish_reason`、SSE `[DONE]`、reasoning 字段过滤和错误归一化。
- API contract：覆盖 JSON 请求、SSE 事件集合、请求级 deadline、队列上限、取消和资源释放。
- 评测 contract：覆盖原文相等、位置正确、未知 evidence、重复文本、`not_found` 和协议失败；未运行的语义指标显式标记为 `not_run`。
- 工作台 contract：覆盖最小提交、清空、错误显示和诊断字段，不持久化原始正文。

## 离线回归

在当前 `aitest` 环境中运行：

```bash
conda run -n aitest python -m pytest -q
```

结果为 `101 passed, 7 skipped`。7 个跳过项是没有在该进程注入 `POSTGRES_DSN` 时的 PostgreSQL 集成测试；它们不是通过，也不能替代真实 PostgreSQL 证据。Ruff、`compileall` 和 `git diff --check` 的复现命令见 [F2-G 复现步骤](../operations/f2-g-reproduction.md)。

## 真实 LM Studio 结果

实验日期为 **2026 年 9 月 14 日**，模型为：

`qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`

服务地址为 `http://localhost:1234/v1`。`/v1/models` 返回 19 个模型，目标模型存在。每个配置包含 JSON/SSE 两种模式、并发 1/2、每组 10 条正常样本、1 条每模式 warmup，以及 2 条独立主动取消样本；温度固定为 `0.0`。

### `max_tokens=128`

- 正常样本：40 条；成功 0 条，失败 40 条；全部 `finish_reason=length`，统一归类为 `truncated`。
- JSON usage：20/20 可见；SSE usage：20/20 缺失。usage 缺失时不推算 token 吞吐。
- JSON 并发 1：完成耗时 P50/P95 为 `9253.48/18566.58 ms`；并发 2 为 `7563.00/12674.50 ms`。
- SSE 并发 1：完成耗时 P50/P95 为 `9378.25/18733.84 ms`；并发 2 为 `7582.81/12750.81 ms`。
- 因没有成功样本，成功延迟和完成吞吐均记为 `null`/`0`，不能把失败请求的耗时当作成功性能。

### `max_tokens=256`

- 正常样本：40 条；成功 40 条，失败 0 条；全部 `finish_reason=stop`。
- 应用层终态：39 条 `answer_status=answered` 且 citation identity `passed`；1 条合法 `answer_status=abstained`，citation 为 `not_applicable`。
- JSON 并发 1：完成耗时 P50/P95 为 `12562.76/25148.48 ms`，完成吞吐约 `0.398 req/s`；并发 2 为 `10294.49/17731.09 ms`，约 `0.564 req/s`。
- SSE 并发 1：完成耗时 P50/P95 为 `12565.76/25189.08 ms`，约 `0.397 req/s`；TTFT P50/P95 为 `12100.79/24725.48 ms`，usage 20 条均缺失。
- SSE 并发 2：完成耗时 P50/P95 为 `9721.23/17559.15 ms`，约 `0.569 req/s`；TTFT P50/P95 为 `9419.77/16948.86 ms`，usage 20 条均缺失。

### 主动取消

两个配置各执行 2 次 SSE 主动取消，均观察到 `cancelled`：

- `max_tokens=128`：completion P50/P95 `1001.09/1002.48 ms`，cleanup P50/P95 `0.57/0.63 ms`。
- `max_tokens=256`：completion P50/P95 `1002.25/1002.66 ms`，cleanup P50/P95 `0.62/0.74 ms`。

cleanup 完成只证明应用侧迭代器、lease 和任务资源完成回收，不证明 LM Studio 上游 GPU 已停止生成。

## 指标解释与边界

- `completion_ms` 从请求开始计时，包含排队时间；`generation_ms` 从获得调度槽位后计时。
- SSE 的 TTFT 定义为请求开始到首个可见、非空正文增量；reasoning 字段和模板包装不计入可见正文。
- P50/P95 使用固定 nearest-rank 算法；每组只有 10 条样本，不能支持稳定容量推论。
- Fake benchmark 仅证明确定性应用层基线（10/10 成功，P50 约 `0.01625 ms`，P95 约 `0.05846 ms`），不能与真实模型延迟合并。
- 当前没有真实检索数据，因此 retrieval recall、citation recall、事实支持、冲突识别和完整 RAG 质量为 `not_applicable` 或 `not_run`。
- 当前未测量 GPU 峰值显存、功耗、成本、上游停止生成语义和生产容量；这些字段在实验产物中明确列为未测量。
- 所有 LM Studio 数字只代表 2026 年 9 月 14 日这台机器、这个 LM Studio 进程、这个模型、这个 Prompt 和这组参数，不能外推为生产 SLO 或容量承诺。

完整原始样本和配置摘要位于：

- `artifacts/experiments/2026-09-14_f2-g/local-benchmark-128.json`
- `artifacts/experiments/2026-09-14_f2-g/local-benchmark-256.json`
- `artifacts/benchmarks/f2-g-fake-2026-09-14.json`
